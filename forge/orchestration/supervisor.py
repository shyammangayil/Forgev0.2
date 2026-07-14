"""
FORGE Supervisor
================
The Supervisor is a stateful conductor that drives the outer
PLAN → EXECUTE → VERIFY → REVIEW → TEST → COMMIT loop.

It is the *only* component that decides:
  - whether to continue to the next phase
  - whether to trigger a repair cycle
  - whether to transition a task to BLOCKED
  - whether to terminate (success, failure, or budget exhaustion)

The Supervisor calls the existing OrchestrationEngine._execute_phase()
and gate functions, but adds:

  1. Per-phase retry budgets (via RetryBudget)
  2. Consensus scoring across Reviewer + Security + QA outputs
  3. Repair-task creation when consensus fails
  4. BLOCKED state transitions when recovery cannot help
  5. Post-task meta-learning hook (LearningEngine.run_full_analysis)

The Supervisor does NOT replace the engine — it wraps the per-phase
execution and replanning logic, providing an auditable trace of every
decision with the reason recorded in the task audit log.

Env vars
--------
  FORGE_CONSENSUS_THRESHOLD   minimum weighted score to pass COMMIT gate (0-1, default 0.65)
  FORGE_AUTO_LEARN            run LearningEngine after every completed task (default true)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from forge.orchestration.budgets import RetryBudget, MAX_PROVIDER_RETRIES
from forge.orchestration.task import Task, TaskResult, TaskState
from forge.telemetry.logger import get_logger
from forge.telemetry.metrics import get_metrics

log     = get_logger("supervisor")
metrics = get_metrics()

_CONSENSUS_THRESHOLD = float(os.environ.get("FORGE_CONSENSUS_THRESHOLD", "0.65"))
_AUTO_LEARN          = os.environ.get("FORGE_AUTO_LEARN", "true").lower() not in ("0", "false", "no")


# ── Consensus scoring ─────────────────────────────────────────────────────────

# Weights and verdict lookup live outside the dataclass so Python's dataclass
# machinery doesn't try to treat them as fields.
_CS_WEIGHTS: Dict[str, float] = {"reviewer": 0.40, "security": 0.35, "qa": 0.25}

_CS_VERDICT_MAP: Dict[str, float] = {
    "APPROVED":      1.0,
    "NEEDS_CHANGES": 0.5,
    "REJECTED":      0.0,
    "CLEAR":         1.0,
    "WARNINGS":      0.5,
    "CRITICAL":      0.0,
    "PASS":          1.0,
    "PARTIAL":       0.5,
    "FAIL":          0.0,
}


@dataclass
class ConsensusScore:
    """
    Aggregate quality signal across the three verification agents.

    Weights:
      reviewer  0.40  — code correctness is the primary gate
      security  0.35  — security is safety-critical
      qa        0.25  — test coverage is important but narrower

    Each agent contributes a sub-score in [0, 1]:
      APPROVED / CLEAR / PASS     → 1.0
      NEEDS_CHANGES / WARNINGS    → 0.5
      REJECTED / CRITICAL / FAIL  → 0.0
      (missing / unparseable)     → 0.3  (partial credit, not total failure)
    """

    reviewer_score:  float = 0.0
    security_score:  float = 0.0
    qa_score:        float = 0.0
    reviewer_detail: str   = ""
    security_detail: str   = ""
    qa_detail:       str   = ""

    @property
    def weighted_score(self) -> float:
        return (
            self.reviewer_score  * _CS_WEIGHTS["reviewer"]
            + self.security_score * _CS_WEIGHTS["security"]
            + self.qa_score       * _CS_WEIGHTS["qa"]
        )

    @property
    def passes(self) -> bool:
        return self.weighted_score >= _CONSENSUS_THRESHOLD

    def summary(self) -> str:
        return (
            f"consensus={self.weighted_score:.2f} "
            f"(reviewer={self.reviewer_score:.2f}, "
            f"security={self.security_score:.2f}, "
            f"qa={self.qa_score:.2f}) "
            f"threshold={_CONSENSUS_THRESHOLD:.2f} → "
            f"{'PASS' if self.passes else 'FAIL'}"
        )

    @classmethod
    def compute(
        cls,
        review:   Optional[Dict],
        security: Optional[Dict],
        qa:       Optional[Dict],
    ) -> "ConsensusScore":
        s = cls()

        def _score(data: Optional[Dict]) -> Tuple[float, str]:
            if not data:
                return 0.3, "(no output)"
            verdict = str(data.get("verdict", "")).upper()
            detail  = data.get("summary") or data.get("reason") or verdict or "(no detail)"
            return _CS_VERDICT_MAP.get(verdict, 0.3), str(detail)[:200]

        s.reviewer_score,  s.reviewer_detail  = _score(review)
        s.security_score,  s.security_detail  = _score(security)
        s.qa_score,        s.qa_detail        = _score(qa)

        return s


# ── Repair task creation ───────────────────────────────────────────────────────

def create_repair_context(
    parent_task: Task,
    phase: str,
    reason: str,
    consensus: Optional[ConsensusScore],
    budget: RetryBudget,
    attempt: int,
) -> Dict[str, Any]:
    """
    Build a structured repair context that is injected into the task's
    context dict.  This is stored in the DB so it survives process restarts
    and is visible in audit logs.

    The repair context replaces the old `replan_feedback` string with a
    richer structured object that includes:
      - which phase failed
      - consensus scores at time of failure
      - remaining budget per phase
      - suggested recovery action
    """
    consensus_data: Dict = {}
    if consensus:
        consensus_data = {
            "score":    round(consensus.weighted_score, 3),
            "reviewer": {"score": consensus.reviewer_score, "detail": consensus.reviewer_detail},
            "security": {"score": consensus.security_score, "detail": consensus.security_detail},
            "qa":       {"score": consensus.qa_score,        "detail": consensus.qa_detail},
            "threshold": _CONSENSUS_THRESHOLD,
        }

    return {
        "repair_attempt":   attempt,
        "failed_phase":     phase,
        "failure_reason":   reason,
        "consensus":        consensus_data,
        "budget_remaining": {
            "implement": budget.remaining("implement"),
            "review":    budget.remaining("review"),
            "test":      budget.remaining("test"),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "directive": (
            f"[Repair #{attempt}] Previous attempt failed at {phase}: {reason}. "
            f"Consensus score was {consensus_data.get('score', 'N/A')}. "
            "You MUST address the specific issues listed above. "
            "Do not repeat the same approach."
        ),
    }


def _is_deterministic_rejection(reason: str) -> bool:
    """
    Return True when a phase failure is caused by a definitive gate verdict
    (REJECTED, CRITICAL, FAIL, secrets detected) rather than a transient
    infrastructure error (LLM timeout, network error, provider unavailable).

    Deterministic failures are not worth retrying — the same agent with the
    same implementation will produce the same verdict.  Only transient
    failures benefit from the repair/retry cycle.
    """
    deterministic_markers = [
        "REJECTED",
        "CRITICAL",
        "QA FAIL",
        "Secrets detected",
        "secrets_detected",
        "No implementation produced",
        "Implementation contains placeholders",
        "Missing phase results",
        "Planning produced no output",
        "No review result",
        "No security review",
        "No QA results",
        "Review REJECTED",
        "Security review returned CRITICAL",
    ]
    reason_upper = reason.upper()
    return any(m.upper() in reason_upper for m in deterministic_markers)


# ── Supervisor ─────────────────────────────────────────────────────────────────

class Supervisor:
    """
    Central conductor for the Forge agent loop.

    The Supervisor owns:
      - the retry budget
      - the consensus gate
      - the repair-cycle decision
      - the BLOCKED transition
      - the post-task learning hook

    It is instantiated once per task run and is designed to be
    checkpointed: all mutable state lives in ``RetryBudget`` (serialisable
    to dict) and the task's ``context`` dict (persisted by the repo).
    """

    def __init__(
        self,
        engine,          # OrchestrationEngine — imported inline to avoid circular import
        task: Task,
        budget: Optional[RetryBudget] = None,
    ):
        self.engine  = engine
        self.task    = task
        self.budget  = budget or RetryBudget()
        self._start  = time.monotonic()

    # ── Main entry point ──────────────────────────────────────────────────

    def run(self) -> TaskResult:
        """
        Execute the full supervised agent loop.

        Loop invariant:
          while not (success or budget_exhausted or blocked):
              execute phases
              check consensus
              if fail: repair and replan
        """
        log.info(
            "Supervisor starting",
            task_id=self.task.id,
            budget=self.budget.to_dict(),
            consensus_threshold=_CONSENSUS_THRESHOLD,
        )
        metrics.inc("forge_supervisor_runs_total")

        result = self._run_loop()

        # ── Post-task meta-learning ───────────────────────────────────────
        if _AUTO_LEARN:
            self._trigger_learning(result)

        return result

    # ── Inner loop ────────────────────────────────────────────────────────

    def _run_loop(self) -> TaskResult:
        engine  = self.engine
        task    = self.task
        repo    = engine.repo

        # Delegate to engine's existing lifecycle but intercept at gate boundaries
        from forge.orchestration.engine import (
            ExecutionContext, ExecutionPhase, PHASE_ORDER,
            RESEARCH_PHASE_ORDER, DEBUGGING_PHASE_ORDER, PHASE_GATES, PERFORMANCE_GATE,
        )

        checkpoint_data = repo.load_latest_checkpoint(task.id)
        ctx = ExecutionContext(task=task)

        if checkpoint_data:
            ctx.current_phase  = ExecutionPhase(checkpoint_data.get("current_phase", "PLAN"))
            ctx.phase_results  = checkpoint_data.get("phase_results", {})
            # Restore budget from checkpoint if present
            if checkpoint_data.get("retry_budget"):
                self.budget = RetryBudget.from_dict(checkpoint_data["retry_budget"])
            log.info(
                "Supervisor resuming from checkpoint",
                task_id=task.id,
                phase=ctx.current_phase.value,
                budget=self.budget.to_dict(),
            )

        base_order = (
            DEBUGGING_PHASE_ORDER
            if task.task_type.value == "debugging"
            else RESEARCH_PHASE_ORDER
            if task.task_type.value == "analysis"
            else PHASE_ORDER
        )

        repair_attempt = task.context.get("repair_attempt", 0)

        while True:
            # ── Check global iteration ceiling ────────────────────────────
            if not self.budget.can_retry("implement") \
               and not self.budget.can_retry("review") \
               and not self.budget.can_retry("test"):
                reason = "All retry budgets exhausted: " + self.budget.exhausted_reason("implement")
                return self._terminate_blocked(ctx, reason)

            if self.budget._total_iterations >= self.budget.max_loop_iterations:
                reason = self.budget.exhausted_reason("implement")
                return self._terminate_blocked(ctx, reason)

            # ── Run phases up to the consensus gate ───────────────────────
            try:
                start_idx = [p.value for p in base_order].index(ctx.current_phase.value)
            except ValueError:
                start_idx = 0

            phases = base_order[start_idx:]
            phase_result = self._run_phases(ctx, phases, PHASE_GATES, PERFORMANCE_GATE)

            if phase_result == "COMPLETE":
                # All phases passed — compute final consensus
                consensus = ConsensusScore.compute(
                    ctx.phase_results.get("review"),
                    ctx.phase_results.get("security"),
                    ctx.phase_results.get("qa"),
                )
                log.info("Final consensus", task_id=task.id, summary=consensus.summary())
                repo.write_audit(task.id, "consensus_computed", "supervisor",
                                 {"score": consensus.weighted_score, "passes": consensus.passes})

                if consensus.passes:
                    return self._build_success(ctx, consensus)

                # Consensus failed.
                # If every agent gave a deterministic verdict (score == 0.0 means
                # REJECTED+CRITICAL+FAIL), there is no point retrying — terminate.
                if consensus.weighted_score == 0.0:
                    reason = f"Consensus hard fail (all agents rejected): {consensus.summary()}"
                    log.error("Supervisor: deterministic consensus failure", task_id=task.id)
                    repo.write_audit(task.id, "consensus_hard_fail", "supervisor",
                                     {"score": 0.0, "summary": consensus.summary()})
                    return self._terminate_failed(ctx, reason)

                # Partial failure — check if we can repair
                phase_key = self._weakest_phase(consensus)
                if not self.budget.can_retry(phase_key):
                    reason = (
                        f"Consensus failed ({consensus.summary()}) and "
                        + self.budget.exhausted_reason(phase_key)
                    )
                    log.error("Budget exhausted after consensus fail", task_id=task.id, reason=reason)
                    return self._terminate_failed(ctx, reason)

                # Consume repair budget and replan
                self.budget.consume(phase_key)
                repair_attempt += 1
                reason = f"Consensus below threshold: {consensus.summary()}"
                self._inject_repair_context(ctx, "CONSENSUS", reason, consensus, repair_attempt)
                self._rewind_to_plan(ctx, base_order)
                repo.save_checkpoint(task.id, f"supervisor_repair_{repair_attempt}",
                                     {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()})
                log.info("Supervisor rewinding for repair", attempt=repair_attempt, task_id=task.id)
                continue

            elif isinstance(phase_result, tuple) and phase_result[0] == "PHASE_FAILED":
                _, failed_phase, reason = phase_result

                # Deterministic gate rejections are not retried.
                if _is_deterministic_rejection(reason):
                    log.info(
                        "Supervisor: deterministic gate rejection — not retrying",
                        phase=failed_phase, reason=reason, task_id=task.id,
                    )
                    repo.write_audit(task.id, "gate_rejection", "supervisor",
                                     {"phase": failed_phase, "reason": reason})
                    return self._terminate_failed(ctx, f"Phase {failed_phase} failed: {reason}")

                # Transient failure — check budget before invoking RecoveryAgent
                # (RecoveryAgent is an LLM call; don't burn it before knowing we can retry)
                phase_key = self._phase_to_budget_key(failed_phase)

                if not self.budget.can_retry(phase_key):
                    # Budget exhausted — terminate as FAILED (not BLOCKED) so the
                    # task state machine matches what existing tests expect for
                    # infrastructure-level failures.
                    reason_full = f"Phase {failed_phase} failed: {reason}. " + self.budget.exhausted_reason(phase_key)
                    repo.write_audit(task.id, "budget_exhausted", "supervisor",
                                     {"phase": failed_phase, "reason": reason_full})
                    return self._terminate_failed(ctx, reason_full)

                # Budget available — now invoke RecoveryAgent and repair
                recovery_plan = self._invoke_recovery_agent(failed_phase, reason)
                log.info("Recovery plan", task_id=task.id, plan=recovery_plan)
                repo.write_audit(task.id, "recovery_invoked", "supervisor",
                                 {"phase": failed_phase, "plan": recovery_plan})

                self.budget.consume(phase_key)
                repair_attempt += 1
                self._inject_repair_context(ctx, failed_phase, reason, None, repair_attempt)
                self._rewind_to_plan(ctx, base_order)
                repo.save_checkpoint(task.id, f"supervisor_repair_{repair_attempt}",
                                     {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()})
                log.info("Supervisor rewinding after transient failure",
                         phase=failed_phase, attempt=repair_attempt, task_id=task.id)
                continue

            else:
                # Unexpected result — terminate
                return self._terminate_failed(ctx, f"Unexpected phase result: {phase_result}")

    # ── Phase runner ──────────────────────────────────────────────────────

    def _run_phases(self, ctx, phases, phase_gates, perf_gate) -> Any:
        """
        Step through phases sequentially.  Returns:
          "COMPLETE"                           — all phases passed
          ("PHASE_FAILED", phase_name, reason) — a gate or agent failed
        """
        from forge.orchestration.engine import ExecutionPhase

        i = 0
        while i < len(phases):
            phase = phases[i]

            if phase == ExecutionPhase.COMPLETE:
                return "COMPLETE"

            # Parallel REVIEW + TEST (inherited from engine)
            if (
                phase == ExecutionPhase.REVIEW
                and i + 1 < len(phases)
                and phases[i + 1] == ExecutionPhase.TEST
                and "security" not in ctx.phase_results
                and "qa"       not in ctx.phase_results
            ):
                log.info("Supervisor: parallel REVIEW+TEST", task_id=self.task.id)
                self.engine.repo.save_checkpoint(
                    self.task.id, "pre_REVIEW_TEST",
                    {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()}
                )
                error = self.engine._execute_parallel_review_test(ctx)
                if error:
                    return ("PHASE_FAILED", "REVIEW+TEST", error)
                self.engine.repo.save_checkpoint(
                    self.task.id, "post_REVIEW_TEST",
                    {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()}
                )
                i += 2
                continue

            # Normal sequential phase
            log.info("Supervisor: phase start", phase=phase.value, task_id=self.task.id)
            ctx.current_phase = phase
            self.engine.repo.save_checkpoint(
                self.task.id, f"pre_{phase.value}",
                {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()}
            )

            success, message = self.engine._execute_phase(phase, ctx)

            if not success:
                return ("PHASE_FAILED", phase.value, message)

            self.engine.repo.save_checkpoint(
                self.task.id, f"post_{phase.value}",
                {**ctx.checkpoint(), "retry_budget": self.budget.to_dict()}
            )
            log.info("Supervisor: phase done", phase=phase.value, task_id=self.task.id)
            i += 1

        return "COMPLETE"

    # ── Recovery ──────────────────────────────────────────────────────────

    def _invoke_recovery_agent(self, phase: str, reason: str) -> dict:
        """Call RecoveryAgent and return its structured plan (or a fallback dict)."""
        try:
            agent  = self.engine.agents.get("recovery")
            if not agent:
                return {"note": "no recovery agent configured"}
            task   = self.task
            # Give recovery agent the failure context
            task.context["recovery_trigger"] = {
                "phase":  phase,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            output = agent.run(task)
            if output.success and output.structured:
                return output.structured
            return {"raw": output.content[:500]}
        except Exception as exc:
            log.warning("Recovery agent failed", error=str(exc))
            return {"error": str(exc)}

    def _inject_repair_context(
        self,
        ctx,
        phase: str,
        reason: str,
        consensus: Optional[ConsensusScore],
        attempt: int,
    ) -> None:
        """Write structured repair context into task.context and ctx.task.context."""
        repair_ctx = create_repair_context(
            self.task, phase, reason, consensus, self.budget, attempt
        )
        # Preserve the directive string in the legacy key so existing agent prompts
        # that check `replan_feedback` still pick it up
        ctx.task.context["replan_feedback"]   = repair_ctx["directive"]
        ctx.task.context["repair_context"]     = repair_ctx
        ctx.task.context["repair_attempt"]     = attempt
        self.engine.repo.write_audit(
            self.task.id, "repair_context_injected", "supervisor",
            {"attempt": attempt, "phase": phase, "consensus_score":
             round(consensus.weighted_score, 3) if consensus else None}
        )

    def _rewind_to_plan(self, ctx, base_order) -> None:
        """Clear downstream phase results and reset the phase pointer to PLAN."""
        from forge.orchestration.engine import ExecutionPhase
        for key in ("implementation", "review", "security", "qa", "documentation"):
            ctx.phase_results.pop(key, None)
        ctx.current_phase = ExecutionPhase.PLAN

    # ── Terminal transitions ───────────────────────────────────────────────

    def _terminate_blocked(self, ctx, reason: str) -> TaskResult:
        """Transition task to BLOCKED (budget exhausted, external dependency)."""
        log.error("Supervisor: task BLOCKED", task_id=self.task.id, reason=reason)
        self.task.transition(TaskState.BLOCKED, reason)
        self.engine.repo.save(self.task)
        self.engine.repo.write_audit(self.task.id, "task_blocked", "supervisor", {"reason": reason})
        metrics.inc("forge_tasks_blocked_total")
        return TaskResult(
            success=False,
            output=ctx.phase_results,
            error=f"BLOCKED: {reason}",
            provider_used=ctx.phase_results.get("last_provider", ""),
        )

    def _terminate_failed(self, ctx, reason: str) -> TaskResult:
        """Transition task to FAILED (unrecoverable error)."""
        log.error("Supervisor: task FAILED", task_id=self.task.id, reason=reason)
        self.engine.repo.write_audit(self.task.id, "task_failed", "supervisor", {"reason": reason})
        metrics.inc("forge_tasks_failed_total")
        return TaskResult(
            success=False,
            output=ctx.phase_results,
            error=reason,
            provider_used=ctx.phase_results.get("last_provider", ""),
        )

    def _build_success(self, ctx, consensus: ConsensusScore) -> TaskResult:
        """Build the final TaskResult after all gates + consensus pass."""
        implementation = ctx.phase_results.get("implementation", "")
        qa_result      = ctx.phase_results.get("qa", {})
        review_result  = ctx.phase_results.get("review", {})
        sec_result     = ctx.phase_results.get("security", {})
        documentation  = ctx.phase_results.get("documentation", "")

        self.engine._write_workspace(self.task, implementation, documentation)
        self.engine._save_task_memories(self.task, implementation, sec_result, qa_result)
        self.engine._notify_slack_complete(self.task, success=True)

        metrics.inc("forge_tasks_completed_total")
        log.info(
            "Supervisor: task complete",
            task_id=self.task.id,
            consensus=consensus.weighted_score,
            duration_s=round(time.monotonic() - self._start, 1),
        )

        return TaskResult(
            success=True,
            output={
                "implementation": implementation,
                "review":         review_result,
                "security":       sec_result,
                "qa":             qa_result,
                "documentation":  documentation,
                "consensus":      {
                    "score":    round(consensus.weighted_score, 3),
                    "reviewer": consensus.reviewer_score,
                    "security": consensus.security_score,
                    "qa":       consensus.qa_score,
                },
            },
            tokens_used=ctx.phase_results.get("total_tokens", 0),
            provider_used=ctx.phase_results.get("last_provider", ""),
            validation_passed=True,
            review_passed=True,
            tests_passed=qa_result.get("verdict") in ("PASS", "PARTIAL"),
        )

    # ── Meta-learning hook ─────────────────────────────────────────────────

    def _trigger_learning(self, result: TaskResult) -> None:
        """
        Post-task hook: run LearningEngine (statistical patterns) AND
        SelfTutor (engineering principle extraction) after every task.
        Both are non-fatal — failures are logged, never surface to the task.
        """
        # ── LearningEngine: failure patterns, model profiles, playbooks ──
        try:
            from forge.learning.patterns import LearningEngine
            le      = LearningEngine()
            summary = le.run_full_analysis(persist=True)
            log.info(
                "Post-task learning analysis complete",
                task_id         = self.task.id,
                tasks_analysed  = summary.get("tasks_analysed", 0),
                failure_patterns= summary.get("failure_patterns", 0),
                best_practices  = summary.get("best_practices", 0),
                model_profiles  = summary.get("model_profiles", 0),
            )
            metrics.inc("forge_learning_runs_total")
        except Exception as exc:
            log.warning("Post-task learning failed (non-fatal)", error=str(exc))

        # ── SelfTutor: engineering principle extraction ───────────────────
        try:
            from forge.tutor import get_tutor
            tutor   = get_tutor()
            updated = tutor.learn_from_task(self.task, result)
            if updated:
                log.info(
                    "Self-tutor: principles updated",
                    task_id    = self.task.id,
                    principles = len(updated),
                    core       = sum(1 for p in updated if p.core),
                )
            metrics.inc("forge_tutor_runs_total")
        except Exception as exc:
            log.warning("Self-tutor post-task failed (non-fatal)", error=str(exc))

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _phase_to_budget_key(phase: str) -> str:
        _MAP = {
            "EXECUTE":    "implement",
            "VERIFY":     "review",
            "REVIEW":     "review",
            "TEST":       "test",
            "REVIEW+TEST":"test",
            "CONSENSUS":  "review",
        }
        return _MAP.get(phase.upper(), "implement")

    @staticmethod
    def _weakest_phase(consensus: ConsensusScore) -> str:
        """Return the budget key for the lowest-scoring verification phase."""
        scores = {
            "review": consensus.reviewer_score,
            "review": consensus.security_score,   # noqa: F601 — overwritten below
            "test":   consensus.qa_score,
        }
        # Keep separate for clarity
        phase_scores = [
            ("review", consensus.reviewer_score + consensus.security_score),
            ("test",   consensus.qa_score),
        ]
        return min(phase_scores, key=lambda x: x[1])[0]
