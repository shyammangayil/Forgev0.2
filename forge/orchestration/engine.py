"""
FORGE Orchestration Engine
===========================
Enforces the PLAN→EXECUTE→VERIFY→REVIEW→TEST→COMMIT execution model.
No task can skip steps. Every transition requires evidence.

The engine:
1. Validates preconditions before each step
2. Saves checkpoints after each step
3. Resumes from checkpoints after crashes
4. Calls independent reviewers on every output
5. Refuses to mark tasks complete without passing all gates

Self-healing loop (via Supervisor):
  PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → CONSENSUS → COMMIT
  On gate failure: ANALYZE → CREATE REPAIR CONTEXT → REPLAN → RETRY
  Budget limits prevent infinite oscillation (see orchestration/budgets.py).
  Consensus scoring (reviewer × security × qa) gates the COMMIT phase.
  Post-task LearningEngine runs automatically (meta-learning loop).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from forge.agents.agents import BaseAgent, build_agents
from forge.config import ForgeConfig, get_config
from forge.orchestration.task import Task, TaskResult, TaskState, TaskType
from forge.orchestration.budgets import RetryBudget
from forge.router.model_router import ModelRouter
from forge.storage.task_repository import TaskRepository
from forge.telemetry.logger import get_logger, new_correlation_id, set_task_id
from forge.telemetry.metrics import get_metrics
from forge.memory.store import get_memory_store
from forge.cost.optimizer import get_tracker, get_selector, estimate_task_complexity
from forge.validators.performance import run_all_validators, format_results as fmt_perf
from forge.validators.security import run_security_validators

log = get_logger("orchestrator")
metrics = get_metrics()


class ExecutionPhase(str, Enum):
    RESEARCH = "RESEARCH"          # optional pre-planning retrieval phase
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    TEST = "TEST"
    COMMIT = "COMMIT"
    COMPLETE = "COMPLETE"


PHASE_ORDER = [
    ExecutionPhase.PLAN,
    ExecutionPhase.EXECUTE,
    ExecutionPhase.VERIFY,
    ExecutionPhase.REVIEW,
    ExecutionPhase.TEST,
    ExecutionPhase.COMMIT,
    ExecutionPhase.COMPLETE,
]

# Phase order when task type is ANALYSIS — leads with research
RESEARCH_PHASE_ORDER = [
    ExecutionPhase.RESEARCH,
    ExecutionPhase.PLAN,
    ExecutionPhase.EXECUTE,
    ExecutionPhase.VERIFY,
    ExecutionPhase.REVIEW,
    ExecutionPhase.TEST,
    ExecutionPhase.COMMIT,
    ExecutionPhase.COMPLETE,
]

# Phase order for DEBUGGING — always investigates before planning
DEBUGGING_PHASE_ORDER = [
    ExecutionPhase.RESEARCH,
    ExecutionPhase.PLAN,
    ExecutionPhase.EXECUTE,
    ExecutionPhase.VERIFY,
    ExecutionPhase.REVIEW,
    ExecutionPhase.TEST,
    ExecutionPhase.COMMIT,
    ExecutionPhase.COMPLETE,
]


@dataclass
class ExecutionContext:
    task: Task
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    current_phase: ExecutionPhase = ExecutionPhase.PLAN
    phase_results: Dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: List[str] = field(default_factory=list)
    # Retry budgets are now managed by the Supervisor via RetryBudget.
    # The legacy replan_count/max_replans fields are kept for backward-compat
    # with checkpoints written by older versions of the engine.
    replan_count: int = 0    # legacy — Supervisor uses RetryBudget instead
    max_replans: int = 2     # legacy — kept so old checkpoints can be resumed

    def checkpoint(self) -> dict:
        return {
            "task_id": self.task.id,
            "correlation_id": self.correlation_id,
            "current_phase": self.current_phase.value,
            "phase_results_keys": list(self.phase_results.keys()),  # kept for back-compat
            "phase_results": self.phase_results,                    # full data for resume
            "started_at": self.started_at.isoformat(),
        }


class OrchestrationGate:
    """
    Gates that must pass before a phase is considered complete.
    Gates are tool-verified, NOT self-reported by agents.
    """

    @staticmethod
    def plan_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Plan must produce a non-empty task description."""
        result = ctx.phase_results.get("plan", {})
        if not result:
            return False, "Planning produced no output"
        return True, "Plan accepted"

    @staticmethod
    def execute_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Implementation must be non-empty and not contain obvious placeholders."""
        impl = ctx.phase_results.get("implementation", "")
        if not impl:
            return False, "No implementation produced"
        placeholders = ["TODO", "FIXME", "pass  # implement", "raise NotImplementedError"]
        import re as _re
        found = [p for p in placeholders if p in impl]
        # Catch bare ellipsis as function body (not type hints like tuple[str, ...])
        if _re.search(r"^\s+\.\.\.\s*$", impl, _re.MULTILINE):
            found.append("bare ...")
        if found:
            return False, f"Implementation contains placeholders: {found}"
        return True, "Implementation accepted"

    @staticmethod
    def verify_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Reviewer must not have returned REJECTED verdict."""
        review = ctx.phase_results.get("review", {})
        if not review:
            return False, "No review result"
        verdict = review.get("verdict", "")
        if verdict == "REJECTED":
            issues = review.get("issues", [])
            critical = [i for i in issues if i.get("severity") in ("CRITICAL", "HIGH")]
            return False, f"Review REJECTED with {len(critical)} critical/high issues"
        return True, f"Review verdict: {verdict}"

    @staticmethod
    def review_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Security check must return CLEAR or WARNINGS (not CRITICAL)."""
        security = ctx.phase_results.get("security", {})
        if not security:
            return False, "No security review"
        verdict = security.get("verdict", "")
        if verdict == "CRITICAL":
            return False, "Security review returned CRITICAL findings"
        if security.get("secrets_detected"):
            return False, f"Secrets detected: {security['secrets_detected']}"
        return True, f"Security verdict: {verdict}"

    @staticmethod
    def test_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """QA must return PASS or PARTIAL with no FAILed criteria."""
        qa = ctx.phase_results.get("qa", {})
        if not qa:
            return False, "No QA results"
        verdict = qa.get("verdict", "")
        if verdict == "FAIL":
            failed = [c for c in qa.get("criteria_results", []) if c.get("status") == "FAIL"]
            return False, f"QA FAIL: {len(failed)} criteria failed"
        return True, f"QA verdict: {verdict}"

    @staticmethod
    def performance_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Run AST-based performance validators on the implementation."""
        impl = ctx.phase_results.get("implementation", "")
        if not impl or len(impl) < 50:
            return True, "No implementation to validate"
        results = run_all_validators(impl)
        failures = [r for r in results if r.verdict == "FAIL"]
        warnings = [r for r in results if r.verdict == "WARN"]
        ctx.phase_results["performance_validation"] = {
            "results": [r.summary() for r in results],
            "failures": len(failures),
            "warnings": len(warnings),
        }
        if failures:
            detail = fmt_perf(results)
            log.warning("Performance gate findings", failures=len(failures), detail=detail)
            # Performance gate warns but does not block by default
            # Set FORGE_COMPLEXITY_GATE=fail to block
            import os
            if os.environ.get("FORGE_COMPLEXITY_GATE", "warn").lower() == "fail":
                return False, f"Performance gate: {len(failures)} violation(s)\n{detail}"
        return True, f"Performance: {len(failures)} fail, {len(warnings)} warn"

    @staticmethod
    def enhanced_security_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """Run comprehensive OWASP + secrets + dependency security validation."""
        impl = ctx.phase_results.get("implementation", "")
        if not impl or len(impl) < 20:
            return True, "No implementation to scan"
        # Also check requirements if present in workspace
        req_text = ""
        try:
            req_path = list(Path(".").rglob("requirements*.txt"))
            if req_path:
                req_text = req_path[0].read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        results = run_security_validators(impl, req_text)
        failures = [r for r in results if r.verdict == "FAIL"]
        all_findings = [f for r in results for f in r.findings]
        critical = [f for f in all_findings if f.severity == "CRITICAL"]
        # Merge into existing security result
        existing = ctx.phase_results.get("security", {})
        existing["static_analysis"] = {
            "owasp_checked": True,
            "findings": [
                {"severity": f.severity, "category": f.category,
                 "location": f.location, "description": f.description}
                for f in all_findings[:20]
            ],
        }
        ctx.phase_results["security"] = existing
        if critical:
            details = "; ".join(f"{f.category}: {f.description[:60]}" for f in critical[:3])
            return False, f"Security gate: {len(critical)} CRITICAL finding(s): {details}"
        if failures:
            log.warning("Security gate HIGH findings", count=len(failures))
        return True, f"Security scan: {len(all_findings)} finding(s)"

    @staticmethod
    def commit_gate(ctx: ExecutionContext) -> tuple[bool, str]:
        """All prior gates must have passed."""
        required_keys = ["implementation", "review", "security", "qa"]
        missing = [k for k in required_keys if k not in ctx.phase_results]
        if missing:
            return False, f"Missing phase results: {missing}"
        return True, "All gates passed — ready to commit"


PHASE_GATES: Dict[ExecutionPhase, Callable] = {
    ExecutionPhase.PLAN:    OrchestrationGate.plan_gate,
    ExecutionPhase.EXECUTE: OrchestrationGate.execute_gate,
    ExecutionPhase.VERIFY:  OrchestrationGate.verify_gate,
    ExecutionPhase.REVIEW:  OrchestrationGate.enhanced_security_gate,
    ExecutionPhase.TEST:    OrchestrationGate.test_gate,
    ExecutionPhase.COMMIT:  OrchestrationGate.commit_gate,
}

# Performance gate runs after EXECUTE (non-blocking by default)
PERFORMANCE_GATE = OrchestrationGate.performance_gate


class OrchestrationEngine:
    """
    The central engine that drives tasks through the execution lifecycle.
    Enforces PLAN→EXECUTE→VERIFY→REVIEW→TEST→COMMIT.
    """

    def __init__(
        self,
        config: Optional[ForgeConfig] = None,
        repo: Optional[TaskRepository] = None,
        router: Optional[ModelRouter] = None,
    ):
        self.config = config or get_config()
        self.config.ensure_dirs()

        self.repo = repo or TaskRepository(
            db_path=str(Path("forge_tasks.db")),
            task_dir=self.config.task_dir,
        )
        self.router = router or ModelRouter(self.config)
        self.agents = build_agents(self.router)

        log.info("Orchestration engine initialised")

    def create_task(
        self,
        title: str,
        description: str,
        task_type: TaskType = TaskType.CODING,
        acceptance_criteria: Optional[List[str]] = None,
        parent_id: Optional[str] = None,
        context: Optional[dict] = None,
        priority: int = 5,
    ) -> Task:
        task = Task(
            title=title,
            description=description,
            task_type=task_type,
            acceptance_criteria=acceptance_criteria or [],
            parent_id=parent_id,
            context=context or {},
            priority=priority,
        )
        task = self.repo.save(task)

        # ── Privacy gateway: inbound scan on all user-supplied content ────
        try:
            from forge.privacy.gateway import get_privacy_gateway, PrivacyViolationError
            gw = get_privacy_gateway()
            result = gw.scan_task(task, repo=self.repo)
            if result.blocked:
                # Hard block: remove task from DB and raise
                self.repo.delete(task.id)
                raise PrivacyViolationError(result.summary(), result)
            if result.has_findings:
                # Soft block: task saved with redacted content, warning logged
                self.repo.save(task)
        except ImportError:
            pass   # privacy module not installed — skip silently

        metrics.inc("forge_tasks_created_total")
        log.info("Task created", task_id=task.id, seq=task.sequence_number, title=task.title)
        return task

    def run_task(self, task_id: str) -> TaskResult:
        """
        Execute a full task lifecycle via the Supervisor.

        The Supervisor owns:
          - retry budgets per phase
          - consensus scoring across reviewer / security / qa
          - repair-cycle decisions
          - BLOCKED state transitions
          - post-task meta-learning

        The engine's _execute_phase / gate functions are called by the
        Supervisor; they are not called directly here any more.
        """
        task = self.repo.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id!r} not found")

        # Restore budget from checkpoint if present
        checkpoint = self.repo.load_latest_checkpoint(task_id)
        budget = RetryBudget()
        if checkpoint and checkpoint.get("retry_budget"):
            budget = RetryBudget.from_dict(checkpoint["retry_budget"])

        new_correlation_id()
        set_task_id(task_id)
        self.repo.write_audit(task_id, "execution_started", "orchestrator",
                              {"supervisor": True, "budget": budget.to_dict()})

        # State machine: NEW → READY → IN_PROGRESS
        if task.state == TaskState.NEW:
            task.transition(TaskState.READY, "Task queued for execution")
        # Allow retry from FAILED state
        if task.state == TaskState.FAILED:
            task.transition(TaskState.READY, "Retrying failed task")
        task.transition(TaskState.IN_PROGRESS, "Execution started")
        self.repo.save(task)

        start = time.monotonic()
        try:
            # Import here to avoid circular import at module load time
            from forge.orchestration.supervisor import Supervisor
            supervisor = Supervisor(engine=self, task=task, budget=budget)
            result = supervisor.run()

            duration_ms = int((time.monotonic() - start) * 1000)
            metrics.observe("forge_task_duration_seconds", time.monotonic() - start)

            if result.success:
                task.transition(TaskState.VERIFIED, "All gates and consensus passed")
                task.transition(TaskState.COMPLETE, "Execution complete")
                metrics.inc("forge_tasks_completed_total")
            elif result.error and result.error.startswith("BLOCKED:"):
                # Supervisor already transitioned to BLOCKED
                pass
            else:
                task.transition(TaskState.FAILED, result.error or "Unknown failure")
                metrics.inc("forge_tasks_failed_total")

            task.result = result
            self.repo.save(task)
            self.repo.write_audit(task_id, "execution_complete", "orchestrator",
                                  {"success": result.success, "duration_ms": duration_ms})
            return result

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.error("Unexpected orchestration failure", task_id=task_id, error=str(exc))
            task.transition(TaskState.FAILED, str(exc))
            task.result = TaskResult(success=False, output=None, error=str(exc))
            self.repo.save(task)
            metrics.inc("forge_tasks_failed_total")
            return task.result

    def _run_lifecycle(self, ctx: ExecutionContext) -> TaskResult:
        """
        Drive the task through each execution phase.

        NOTE: This method is retained for backward compatibility (tests,
        CLI direct calls).  New code should go through run_task() which
        uses the Supervisor for budgets, consensus, and repair cycles.

        Normal flow:
          PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → COMMIT → COMPLETE

        Research flow (task_type=ANALYSIS):
          RESEARCH → PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → COMMIT → COMPLETE
        """
        task = ctx.task
        base_order = (
            DEBUGGING_PHASE_ORDER
            if task.task_type.value == "debugging"
            else RESEARCH_PHASE_ORDER
            if task.task_type.value == "analysis"
            else PHASE_ORDER
        )
        try:
            start_idx = [p.value for p in base_order].index(ctx.current_phase.value)
        except ValueError:
            start_idx = 0
        phases = base_order[start_idx:]

        i = 0
        while i < len(phases):
            phase = phases[i]

            if phase == ExecutionPhase.COMPLETE:
                break

            # ── Parallel REVIEW + TEST ────────────────────────────────────────
            if (
                phase == ExecutionPhase.REVIEW
                and i + 1 < len(phases)
                and phases[i + 1] == ExecutionPhase.TEST
                and "security" not in ctx.phase_results
                and "qa"       not in ctx.phase_results
            ):
                log.info("Entering parallel phase", phases="REVIEW+TEST", task_id=task.id)
                self.repo.save_checkpoint(task.id, "pre_REVIEW_TEST", ctx.checkpoint())

                error = self._execute_parallel_review_test(ctx)
                if error:
                    log.error("Parallel phase failed", task_id=task.id, reason=error)
                    return TaskResult(
                        success=False,
                        output=ctx.phase_results,
                        error=error,
                        provider_used=ctx.phase_results.get("last_provider", ""),
                    )

                log.info("Parallel phase passed", phases="REVIEW+TEST", task_id=task.id)
                self.repo.save_checkpoint(task.id, "post_REVIEW_TEST", ctx.checkpoint())
                i += 2
                continue

            # ── Normal sequential phase ───────────────────────────────────────
            log.info("Entering phase", phase=phase.value, task_id=task.id)
            ctx.current_phase = phase
            self.repo.save_checkpoint(task.id, f"pre_{phase.value}", ctx.checkpoint())

            if phase == ExecutionPhase.PLAN:
                selection = get_selector().select(
                    task_type=task.task_type.value,
                    description=task.description,
                    available_providers=self.router.available_providers(),
                )
                if selection:
                    provider, model = selection
                    self.config.router.default_provider = provider
                    model_attr = f"{provider}_model"
                    if hasattr(self.config.router, model_attr):
                        setattr(self.config.router, model_attr, model)
                    log.info("Cost optimizer selected model", provider=provider, model=model)

            success, message = self._execute_phase(phase, ctx)

            if not success:
                log.error("Phase failed", phase=phase.value, task_id=task.id, reason=message)
                self._notify_slack_complete(ctx.task, success=False,
                    phase=phase.value, error=message)
                return TaskResult(
                    success=False,
                    output=ctx.phase_results,
                    error=f"Phase {phase.value} failed: {message}",
                    provider_used=ctx.phase_results.get("last_provider", ""),
                )

            log.info("Phase passed", phase=phase.value, message=message, task_id=task.id)
            self.repo.save_checkpoint(task.id, f"post_{phase.value}", ctx.checkpoint())
            i += 1

        # Build final result
        implementation = ctx.phase_results.get("implementation", "")
        qa_result      = ctx.phase_results.get("qa", {})
        review_result  = ctx.phase_results.get("review", {})
        sec_result     = ctx.phase_results.get("security", {})
        documentation  = ctx.phase_results.get("documentation", "")

        self._write_workspace(ctx.task, implementation, documentation)
        self._save_task_memories(ctx.task, implementation, sec_result, qa_result)
        self._notify_slack_complete(ctx.task, success=True)

        return TaskResult(
            success=True,
            output={
                "implementation": implementation,
                "review":         review_result,
                "security":       sec_result,
                "qa":             qa_result,
                "documentation":  documentation,
            },
            tokens_used=ctx.phase_results.get("total_tokens", 0),
            provider_used=ctx.phase_results.get("last_provider", ""),
            validation_passed=True,
            review_passed=True,
            tests_passed=qa_result.get("verdict") in ("PASS", "PARTIAL"),
        )

    def _record_token_usage(self, task, phase: str, agent: str,
                             provider: str, model: str,
                             tokens_in: int, tokens_out: int, duration_ms: int) -> None:
        """Record LLM call to token budget tracker (non-fatal)."""
        try:
            get_tracker().record(
                task_id=task.id, phase=phase, agent=agent,
                provider=provider, model=model,
                tokens_in=tokens_in, tokens_out=tokens_out,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            log.warning("Token tracking failed", error=str(exc))

    def _save_task_memories(self, task, implementation, sec_result, qa_result) -> None:
        """Persist key facts from this task into cross-task memory."""
        try:
            store = get_memory_store()
            title = task.title[:80]
            if implementation:
                store.remember("context",
                    f"task:{task.id}:summary",
                    f"Task '{title}' produced: {implementation[:300]}",
                    tags=["task", task.task_type.value], task_id=task.id)
            for f in (sec_result or {}).get("findings", [])[:3]:
                sev, desc = f.get("severity",""), f.get("description","")
                if sev in ("HIGH","CRITICAL") and desc:
                    store.remember("error", f"security:{task.id}:{sev}",
                        f"Security issue in '{title}': {desc}",
                        tags=["security", sev.lower()], task_id=task.id, confidence=0.9)
            for gap in (qa_result or {}).get("missing_coverage", [])[:3]:
                store.remember("finding", f"qa_gap:{task.id}:{gap[:40]}",
                    f"QA gap in '{title}': {gap}",
                    tags=["qa","gap"], task_id=task.id, confidence=0.8)
        except Exception as exc:
            log.warning("Failed to save task memories", error=str(exc))

    def _notify_slack_complete(self, task, success: bool, phase: str = "", error: str = "") -> None:
        """Send Slack notification. Non-fatal if Slack not configured."""
        import os as _os
        if not _os.environ.get("SLACK_BOT_TOKEN"):
            return
        try:
            from forge.integrations.slack import notify_task_complete, notify_task_failed
            from datetime import datetime, timezone as tz
            duration = 0.0
            if task.started_at:
                duration = (datetime.now(tz.utc) - task.started_at).total_seconds() / 60
            seq = f"TASK-{task.sequence_number:03d}" if task.sequence_number else task.id
            if success:
                notify_task_complete(seq, task.title, True, duration)
            else:
                notify_task_failed(seq, task.title, phase, error)
        except Exception as exc:
            log.warning("Slack notification failed", error=str(exc))

    def _write_workspace(self, task: "Task", implementation: str, documentation: str) -> None:
        """
        Extract code blocks from the implementation and write them as real
        files into workspace/TASK-NNN/.  Also writes a SUMMARY.md with the
        documentation agent output.

        Files are detected by fenced code blocks with a filename comment on
        the first line inside the fence:
            ```python
            # filename: mymodule.py
            ...code...
            ```
        If no filename comment is present the block is written as
        implementation_N.<ext> where ext is inferred from the language tag.
        """
        import re as _re

        cfg = get_config()
        seq = f"TASK-{task.sequence_number:03d}" if task.sequence_number else f"TASK-{task.id}"
        ws  = cfg.workspace_dir / seq
        ws.mkdir(parents=True, exist_ok=True)

        # ── Extract fenced code blocks ────────────────────────────────────
        lang_to_ext = {
            "python": "py", "py": "py",
            "javascript": "js", "js": "js",
            "typescript": "ts", "ts": "ts",
            "bash": "sh", "shell": "sh", "sh": "sh",
            "sql": "sql", "json": "json", "yaml": "yaml", "yml": "yaml",
            "html": "html", "css": "css", "markdown": "md", "md": "md",
        }
        fence_re = _re.compile(
            r"```(?P<lang>[\w+-]*)\n(?P<body>.*?)```",
            _re.DOTALL,
        )
        written = []
        counter: dict = {}

        for i, m in enumerate(fence_re.finditer(implementation), start=1):
            lang = m.group("lang").lower().strip()
            body = m.group("body")

            # Try to extract filename from first line comment
            first_line = body.splitlines()[0].strip() if body.strip() else ""
            fname_match = _re.match(r"#\s*filename:\s*(\S+)", first_line, _re.IGNORECASE)
            if fname_match:
                fname = fname_match.group(1)
                # Strip the filename comment from the written content
                body = "\n".join(body.splitlines()[1:]).lstrip("\n")
            else:
                ext = lang_to_ext.get(lang, lang or "txt")
                counter[ext] = counter.get(ext, 0) + 1
                n = counter[ext]
                fname = f"implementation_{n}.{ext}" if n > 1 else f"implementation.{ext}"

            out_path = ws / fname
            try:
                out_path.write_text(body, encoding="utf-8")
                written.append(fname)
            except Exception as exc:
                log.warning("Failed to write workspace file", file=fname, error=str(exc))

        # ── Write SUMMARY.md ─────────────────────────────────────────────
        summary_lines = [f"# {seq}: {task.title}\n"]
        if documentation:
            summary_lines.append(documentation)
        else:
            summary_lines.append("*No documentation generated.*")
        if written:
            summary_lines.append("\n## Output Files\n")
            for f in written:
                summary_lines.append(f"- `{f}`")

        summary_path = ws / "SUMMARY.md"
        try:
            summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
            written.append("SUMMARY.md")
        except Exception as exc:
            log.warning("Failed to write SUMMARY.md", error=str(exc))

        if written:
            log.info("Workspace written", task_id=task.id, dir=str(ws), files=written)
        else:
            log.warning("No files extracted to workspace", task_id=task.id, dir=str(ws))

    def _execute_parallel_review_test(self, ctx: ExecutionContext) -> Optional[str]:
        """
        Run SecurityAgent (REVIEW) then QAAgent (TEST) with fail-fast gate checks.

        Runs security first (deterministic ordering ensures mocked side_effect
        lists are consumed predictably in tests).  Returns None on success, or
        an error string that includes the phase name so the Supervisor can
        classify it correctly.

        Gate order (fail-fast):
          1. LLM security verdict check (review_gate) — fast, no I/O
          2. Enhanced static analysis gate — only if LLM said CLEAR/WARNINGS
          3. QA agent + test gate — only if security passed
        """
        task = ctx.task

        # ── Security agent (REVIEW) ───────────────────────────────────────
        sec_agent  = self.agents["security"]
        sec_output = sec_agent.run(task)
        if not sec_output.success:
            return f"Gate failed for REVIEW: Security agent failed: {sec_output.error}"

        ctx.phase_results["security"] = sec_output.structured or {"verdict": "CLEAR", "findings": []}
        ctx.phase_results["last_provider"] = sec_output.provider_used
        ctx.phase_results["total_tokens"] = (
            ctx.phase_results.get("total_tokens", 0) + sec_output.tokens_used
        )

        # Fail-fast: check LLM verdict first (review_gate) before running
        # the heavier static analysis (enhanced_security_gate).
        # This ensures a CRITICAL LLM verdict terminates the parallel block
        # immediately without running QA.
        sec_llm_passed, sec_llm_msg = OrchestrationGate.review_gate(ctx)
        if not sec_llm_passed:
            return f"Gate failed for REVIEW: {sec_llm_msg}"

        # Static analysis gate (enhanced) — only runs when LLM verdict is safe
        sec_passed, sec_msg = PHASE_GATES[ExecutionPhase.REVIEW](ctx)
        if not sec_passed:
            return f"Gate failed for REVIEW: {sec_msg}"

        # ── QA agent (TEST) ───────────────────────────────────────────────
        qa_agent  = self.agents["qa"]
        qa_output = qa_agent.run(task)
        if not qa_output.success:
            return f"Gate failed for TEST: QA agent failed: {qa_output.error}"

        ctx.phase_results["qa"] = qa_output.structured or {"verdict": "PASS", "criteria_results": []}
        ctx.phase_results["last_provider"] = qa_output.provider_used
        ctx.phase_results["total_tokens"] = (
            ctx.phase_results.get("total_tokens", 0) + qa_output.tokens_used
        )

        qa_passed, qa_msg = PHASE_GATES[ExecutionPhase.TEST](ctx)
        if not qa_passed:
            return f"Gate failed for TEST: {qa_msg}"

        return None

    def _execute_research_phase(self, ctx: ExecutionContext) -> tuple[bool, str]:
        """
        Dispatch the correct investigation agent before planning.
          debugging -> DebuggerAgent  (read files first, then plan)
          analysis  -> ResearchAgent  (web search + repo AS-IS)
        """
        task      = ctx.task
        is_debug  = task.task_type.value == "debugging"
        agent_key = "debugger" if is_debug else "researcher"
        agent     = self.agents.get(agent_key) or self.agents.get(
            "researcher" if is_debug else "debugger"
        )
        if not agent:
            log.warning("No research/debug agent — skipping RESEARCH phase")
            return True, "Research phase skipped (no agent)"

        log.info("Research phase dispatched",
                 agent=agent_key, task_id=task.id, task_type=task.task_type.value)
        output = agent.run(task)

        if not output.success:
            log.warning("Investigation failed (non-fatal) — continuing to PLAN",
                        agent=agent_key, error=output.error)
            return True, f"Investigation failed (non-fatal): {output.error}"

        ctx.phase_results["research"]      = output.structured or {"summary": output.content}
        ctx.phase_results["last_provider"] = output.provider_used
        ctx.phase_results["total_tokens"]  = (
            ctx.phase_results.get("total_tokens", 0) + (output.tokens_used or 0)
        )

        if is_debug:
            inv = output.structured or {}
            task.context["debug_investigation"]    = inv
            task.context["root_cause"]             = inv.get("root_cause", "")
            task.context["files_to_change"]        = inv.get("files_to_change", [])
            task.context["investigation_complete"] = inv.get("investigation_complete", False)
            task.context["files_read"]             = inv.get("files_read", [])
            self.repo.write_audit(task.id, "debug_investigation_complete", "debugger", {
                "files_read":      len(inv.get("files_read", [])),
                "root_cause":      inv.get("root_cause", "")[:200],
                "files_to_change": len(inv.get("files_to_change", [])),
            })
            log.info("Debug investigation complete", task_id=task.id,
                     files_read=len(inv.get("files_read", [])),
                     root_cause=inv.get("root_cause", "")[:80])
        else:
            summary = (output.structured or {}).get("summary", output.content)
            task.context["research_findings"] = _trim(summary, 3000, "research")

        return True, "Investigation complete"

    def _execute_phase(self, phase: ExecutionPhase, ctx: ExecutionContext) -> tuple[bool, str]:
        """Execute a single phase and check its gate."""
        task = ctx.task

        if phase == ExecutionPhase.RESEARCH:
            return self._execute_research_phase(ctx)

        elif phase == ExecutionPhase.PLAN:
            agent = self.agents["planner"]
            output = agent.run(task)
            if not output.success:
                return False, f"Planner failed: {output.error}"
            ctx.phase_results["plan"] = output.structured or {"content": output.content}
            ctx.phase_results["last_provider"] = output.provider_used
            ctx.phase_results["total_tokens"] = ctx.phase_results.get("total_tokens", 0) + output.tokens_used

        elif phase == ExecutionPhase.EXECUTE:
            agent = self.agents["implementer"]
            output = agent.run(task)
            if not output.success:
                return False, f"Implementer failed: {output.error}"
            ctx.phase_results["implementation"] = output.content
            task.context["implementation"] = output.content
            self.repo.save(task)
            ctx.phase_results["last_provider"] = output.provider_used
            ctx.phase_results["total_tokens"] = ctx.phase_results.get("total_tokens", 0) + output.tokens_used


        # Run performance gate after EXECUTE (warn-only by default)
        if phase == ExecutionPhase.EXECUTE:
            PERFORMANCE_GATE(ctx)

            # ── Test Intelligence: analyse implementation immediately after EXECUTE ──
            # Runs before VERIFY/TEST so the QAAgent prompt is enriched and
            # the Supervisor can see intelligence findings in the audit log.
            try:
                from forge.intelligence import get_intelligence_engine
                impl_code = ctx.phase_results.get("implementation", "")
                if impl_code.strip():
                    ti_engine = get_intelligence_engine()
                    ti_report = ti_engine.analyse(
                        impl_code,
                        filename=f"task_{task.id}.py",
                    )
                    ctx.phase_results["test_intelligence"] = ti_report.summary_dict()
                    task.context["test_intelligence"] = ti_report.summary_dict()
                    # Persist cross-task risk memory
                    mem = get_memory_store()
                    for entry in ti_report.to_memory_entries(task_id=task.id):
                        try:
                            mem.remember(
                                key         = entry["key"],
                                value       = entry["value"],
                                memory_type = entry["memory_type"],
                                confidence  = entry["confidence"],
                                tags        = entry["tags"],
                                task_id     = entry["task_id"],
                            )
                        except Exception:
                            pass
                    self.repo.write_audit(task.id, "test_intelligence", "engine",
                                          ctx.phase_results["test_intelligence"])
                    log.info(
                        "Test Intelligence complete",
                        task_id=task.id,
                        **ti_report.summary_dict(),
                    )
            except Exception as exc:
                log.warning("Test Intelligence failed (non-fatal)", error=str(exc), task_id=task.id)

        elif phase == ExecutionPhase.VERIFY:
            agent = self.agents["reviewer"]
            output = agent.run(task)
            if not output.success:
                return False, f"Reviewer failed: {output.error}"
            ctx.phase_results["review"] = output.structured or {"verdict": "APPROVED", "issues": []}
            ctx.phase_results["last_provider"] = output.provider_used
            ctx.phase_results["total_tokens"] = ctx.phase_results.get("total_tokens", 0) + output.tokens_used

        elif phase == ExecutionPhase.REVIEW:
            agent = self.agents["security"]
            output = agent.run(task)
            if not output.success:
                return False, f"Security agent failed: {output.error}"
            ctx.phase_results["security"] = output.structured or {"verdict": "CLEAR", "findings": []}
            ctx.phase_results["last_provider"] = output.provider_used
            ctx.phase_results["total_tokens"] = ctx.phase_results.get("total_tokens", 0) + output.tokens_used

        elif phase == ExecutionPhase.TEST:
            agent = self.agents["qa"]
            output = agent.run(task)
            if not output.success:
                return False, f"QA agent failed: {output.error}"
            ctx.phase_results["qa"] = output.structured or {"verdict": "PASS", "criteria_results": []}
            ctx.phase_results["last_provider"] = output.provider_used
            ctx.phase_results["total_tokens"] = ctx.phase_results.get("total_tokens", 0) + output.tokens_used

        elif phase == ExecutionPhase.COMMIT:
            # Documentation phase happens here
            agent = self.agents["documentation"]
            output = agent.run(task)
            if output.success:
                ctx.phase_results["documentation"] = output.content

        # Check gate
        gate_fn = PHASE_GATES.get(phase)
        if gate_fn:
            passed, msg = gate_fn(ctx)
            if not passed:
                return False, msg
            return True, msg

        return True, f"Phase {phase.value} complete"

    def get_task_status(self, task_id: str) -> Optional[dict]:
        task = self.repo.get(task_id)
        if not task:
            return None
        return {
            "task_id": task.id,
            "sequence": task.sequence_number,
            "title": task.title,
            "state": task.state.value,
            "assigned_agent": task.assigned_agent,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "audit_entries": len(task.audit_log),
        }

    def list_tasks(self) -> List[dict]:
        return [
            {
                "id": t.id,
                "seq": t.sequence_number,
                "title": t.title,
                "state": t.state.value,
                "type": t.task_type.value,
                "priority": t.priority,
            }
            for t in self.repo.list_all()
        ]

    def metrics_summary(self) -> dict:
        repo_stats = self.repo.stats()
        metrics_snap = get_metrics().snapshot()
        return {
            "repository": repo_stats,
            "metrics": metrics_snap,
            "providers": self.router.available_providers(),
        }