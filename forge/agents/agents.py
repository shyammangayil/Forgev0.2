"""
FORGE Agent Layer
=================
Eight specialized agents. Every agent follows the same contract:
  - Accepts a Task
  - Calls the router for LLM completions
  - Returns structured output
  - NEVER marks a task complete — that is the orchestrator's job

Teal agents (generative): Planner, Implementer, Reviewer, QA, Researcher
Coral agents (verification): Security, Documentation, Recovery, Performance

Context Budget
--------------
Each agent trims its input before building prompts to stay within a safe
token budget for local models. The helpers _trim() and _summarise_impl()
are the single place that controls how much context each agent receives.

Tuning env vars (all optional):
  FORGE_CONTEXT_CHARS   max characters of implementation sent to reviewer/qa/security
                        default: 12000  (~3 000 tokens — safe for most 20B models)
  FORGE_PLAN_CHARS      max characters of plan JSON sent to implementer
                        default: 4000
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from forge.orchestration.task import Task, TaskResult, TaskType
from forge.providers.base import CompletionRequest, Message, ToolCall
from forge.router.model_router import ModelRouter
from forge.tools.registry import (
    TOOL_REGISTRY, ToolResult, execute_tool,
    get_tool_schemas_openai, get_tool_schemas_anthropic,
)
from forge.memory.store import get_memory_store
from forge.telemetry.logger import get_logger, set_agent, set_correlation_id
from forge.telemetry.metrics import get_metrics

log = get_logger("agents")
metrics = get_metrics()

# ── Context budget constants (tunable via env) ────────────────────────────────
_CONTEXT_CHARS = int(os.environ.get("FORGE_CONTEXT_CHARS", "12000"))
_PLAN_CHARS    = int(os.environ.get("FORGE_PLAN_CHARS",    "4000"))


@dataclass
class AgentOutput:
    success: bool
    content: str
    structured: Optional[Dict] = None
    tokens_used: int = 0
    provider_used: str = ""
    duration_ms: int = 0
    error: Optional[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


# ── Context trimming helpers ──────────────────────────────────────────────────

def _trim(text: str, max_chars: int, label: str = "content") -> str:
    """
    Hard-trim text to max_chars. When trimming, keeps the first 60% and last
    40% so both the start (imports, class definitions) and end (recent logic)
    are preserved, and inserts a clear marker so the model knows content was
    removed rather than seeing a jagged cut.
    """
    if len(text) <= max_chars:
        return text
    keep_head = int(max_chars * 0.60)
    keep_tail = max_chars - keep_head
    removed = len(text) - max_chars
    marker = (
        f"\n\n... [{label} trimmed: {removed:,} characters removed to fit context budget] ...\n\n"
    )
    trimmed = text[:keep_head] + marker + text[-keep_tail:]
    log.info(
        "Context trimmed",
        label=label,
        original_chars=len(text),
        trimmed_chars=len(trimmed),
        removed_chars=removed,
    )
    return trimmed


def _slim_context(ctx: dict, exclude_keys: tuple = ("implementation",)) -> str:
    """
    Render task.context as JSON but exclude large keys that are passed
    separately (e.g. implementation). Keeps context informative but tiny.
    """
    slim = {k: v for k, v in ctx.items() if k not in exclude_keys}
    # Further trim any remaining long string values
    for k, v in slim.items():
        if isinstance(v, str) and len(v) > 500:
            slim[k] = v[:500] + f"... [truncated, {len(v) - 500} chars omitted]"
    return json.dumps(slim, indent=2)


def _format_criteria(criteria: List[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria) if criteria else "- (none specified)"


# ── System prompts ────────────────────────────────────────────────────────────

PLANNER_SYSTEM = """You are FORGE Planner — a principal software architect.
Your role: decompose requirements into precise, verifiable tasks.

Rules:
- Every task must have clear acceptance criteria
- Tasks must be atomic — one concern per task
- Parent tasks coordinate; child tasks implement
- Never assume — ask if unclear
- Output valid JSON only when asked for structured output

Output format for task breakdown:
{
  "tasks": [
    {
      "title": "short title",
      "description": "detailed description",
      "type": "coding|review|testing|security|documentation|planning",
      "acceptance_criteria": ["criterion 1", "criterion 2"],
      "priority": 1-10,
      "dependencies": ["task title"]
    }
  ]
}"""

IMPLEMENTER_SYSTEM = """You are FORGE Implementer — a principal software engineer.
Your role: write production-grade code.

Standards:
- Type hints on all functions
- Docstrings on all classes and public methods
- Exception handling — never bare except
- Structured logging at key points
- No placeholder implementations unless explicitly requested
- Code must compile and run as written

When producing code, wrap it in triple-backtick fences with language tags.
After the code, include a section "## Verification Steps" listing how to
confirm the code works."""

REVIEWER_SYSTEM = """You are FORGE Reviewer — an adversarial code reviewer.
Your role: challenge every assumption, find every flaw.

Review checklist:
- Correctness: does it do what's claimed?
- Security: injection, auth bypass, secret exposure, input validation
- Reliability: exception handling, resource cleanup, retry logic
- Performance: N+1 queries, unbounded loops, memory leaks
- Maintainability: naming, complexity, coupling
- Hallucinations: does code reference things that don't exist?

Output format:
{
  "verdict": "APPROVED|REJECTED|NEEDS_CHANGES",
  "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "issues": [{"severity": "...", "location": "...", "description": "...", "fix": "..."}],
  "summary": "one paragraph"
}"""

QA_SYSTEM = """You are FORGE QA — an AI-assisted intelligent quality engineer (Gen-4).
Unlike Gen-3 automation tools (Playwright, Cypress, Testim) that record and replay,
you reason about what needs testing based on static analysis intelligence.

You will receive a TEST INTELLIGENCE BRIEF that identifies:
  • Critical Paths  — execution paths that break everything if untested
  • Risk Areas      — high-defect-probability code (SQL injection, bare excepts, etc.)
  • Untested Areas  — functions with no existing test coverage signal
  • Dead Code       — code that can never execute (do NOT write tests for these)

Your role: use the brief to write the most valuable tests first.

For every acceptance criterion AND every item in the brief:
1. State how you would verify it
2. Write a test that proves it (pytest-compatible Python)
3. Describe what failure looks like

Test types to write (in priority order from the brief):
  - Critical path tests: end-to-end through the identified call chains
  - Risk area tests: edge cases, injection attempts, malformed inputs, boundary values
  - Untested area smoke tests: at minimum one happy-path and one failure-path test
  - Criterion verification tests: explicit acceptance criterion coverage

Output format:
{
  "verdict": "PASS|FAIL|PARTIAL",
  "criteria_results": [{"criterion": "...", "status": "PASS|FAIL", "test": "..."}],
  "critical_path_tests": ["list of test function names written"],
  "risk_area_tests": ["list of test function names written"],
  "dead_code_skipped": ["list of locations intentionally NOT tested"],
  "test_code": "full pytest file content",
  "missing_coverage": ["list of remaining gaps after these tests"],
  "intelligence_used": true
}"""

SECURITY_SYSTEM = """You are FORGE Security — a security architect and penetration tester.
Your role: find vulnerabilities before they reach production.

Check for:
- Secrets hardcoded or in logs
- SQL/command/LDAP injection
- Authentication bypass
- Insecure deserialization
- Path traversal
- SSRF, CSRF, XSS
- Dependency vulnerabilities
- Privilege escalation
- Race conditions

Output format:
{
  "verdict": "CLEAR|WARNINGS|CRITICAL",
  "findings": [
    {"cve_like_id": "FORGE-SEC-001", "severity": "...", "description": "...", "remediation": "..."}
  ],
  "secrets_detected": ["list of detected secret patterns"],
  "summary": "one paragraph"
}"""

DOCUMENTATION_SYSTEM = """You are FORGE Documentation — a technical writer.
Your role: produce clear, accurate, maintainable documentation.

Always generate:
- Module/class/function docstrings
- Usage examples
- Configuration reference
- Error codes and their meaning
- Architecture decision records (ADRs) for significant design choices

Documentation must be factual — never document behaviour that doesn't exist."""

PERFORMANCE_SYSTEM = """You are FORGE Performance — a performance engineer.
Your role: identify bottlenecks and recommend optimizations.

Analyse:
- Algorithmic complexity
- Database query patterns
- Memory allocation patterns
- I/O bottlenecks
- Concurrency issues
- Caching opportunities

Output format:
{
  "bottlenecks": [{"location": "...", "severity": "LOW|MEDIUM|HIGH", "description": "...", "fix": "..."}],
  "estimated_improvement": "...",
  "benchmark_recommendations": ["..."]
}"""

RECOVERY_SYSTEM = """You are FORGE Recovery — a site reliability engineer.
Your role: detect failures and design recovery strategies.

When analysing a failure:
1. Identify what checkpoint to resume from
2. Determine if any partial work is salvageable
3. Propose a recovery plan
4. Estimate recovery time

Output format:
{
  "failure_type": "...",
  "last_good_checkpoint": "...",
  "recovery_steps": ["..."],
  "data_loss_risk": "NONE|LOW|MEDIUM|HIGH",
  "estimated_recovery_minutes": 0
}"""


# ── Base agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    agent_name: str = "base"
    task_type: TaskType = TaskType.CODING

    def __init__(self, router: ModelRouter):
        self.router = router

    # Tools this agent is allowed to call. Override in subclasses.
    # None = no tools. List of tool names from TOOL_REGISTRY.
    allowed_tools: Optional[List[str]] = None

    # Maximum tool-call rounds before returning (guards against infinite loops)
    max_tool_rounds: int = 8

    def _inject_memories(self, system: str, prompt: str) -> str:
        """
        Enrich the agent system prompt with two sources of cross-task knowledge:

        1. Memory store — task-specific facts recalled by semantic search
           (failure patterns, best practices, risk findings from prior runs).

        2. Principle store (SelfTutor) — generalised engineering principles
           extracted from execution traces, repository scans, and specs.
           Filtered by agent role so each agent sees the most relevant
           principles (planner sees planning/decomposition/architecture;
           QA sees testing/verification; etc.).

        Both sources are non-fatal — any failure returns the original system
        prompt unchanged.
        """
        enriched = system

        # ── 1. Memory store: task-specific recalled facts ─────────────────
        try:
            store   = get_memory_store()
            query   = prompt[:200].strip()
            entries = store.recall(query, max_results=5)
            if entries:
                memory_block = store.format_for_prompt(entries, max_chars=1500)
                enriched = f"{enriched}\n\n{memory_block}"
        except Exception:
            pass

        # ── 2. Principle store: generalised engineering principles ─────────
        try:
            from forge.tutor import get_tutor
            tutor = get_tutor()
            principle_block = tutor.enrich_prompt(
                "",
                agent_name    = self.agent_name,
                max_principles= 5,
            )
            if principle_block:
                enriched = f"{enriched}\n\n{principle_block}"
        except Exception:
            pass

        return enriched

    def _call_llm(self, system: str, prompt: str, task: Task, max_tokens: int = 4096) -> AgentOutput:
        """
        Call the LLM with an agentic tool-use loop.

        Privacy gateway integration
        ---------------------------
        Every call gets a fresh ``llm_correlation_id`` (UUID4 short string)
        that ties the outbound prompt to the inbound response in the gateway's
        redaction ledger.

        Outbound:
            gw.redact_prompt(system, prompt, correlation_id, task_id)
              → replaces PII/PHI with typed tokens «PII-EMAIL-0» etc.
              → stores token→original map in ledger[correlation_id]
              → raises PrivacyViolationError if gate=fail and HIGH findings

        Inbound (after LLM responds):
            gw.scrub_response(raw_response, correlation_id, task_id)
              → looks up manifest in ledger, consumes it (one-shot)
              → detects residual PII not present in the prompt
              → redacts residual PII
              → returns clean response

        If no gateway or ImportError the call proceeds unchanged.
        """
        set_agent(self.agent_name)
        metrics.inc("forge_agent_invocations_total")
        start = time.monotonic()

        # Generate a per-call correlation id for the privacy ledger
        llm_correlation_id = str(uuid.uuid4())[:8]

        # Inject relevant memories into system prompt
        system = self._inject_memories(system, prompt)

        # ── Privacy gateway: outbound prompt redaction ────────────────────
        _gw = None
        try:
            from forge.privacy.gateway import get_privacy_gateway, PrivacyViolationError
            _gw = get_privacy_gateway()
            system, prompt, _manifest = _gw.redact_prompt(
                system_prompt  = system,
                user_prompt    = prompt,
                correlation_id = llm_correlation_id,
                task_id        = task.id,
            )
        except ImportError:
            pass  # privacy module not installed — proceed without
        except Exception as exc:
            # PrivacyViolationError (gate=fail) surfaces here
            duration_ms = int((time.monotonic() - start) * 1000)
            log.error("Privacy gate blocked prompt", task_id=task.id, error=str(exc))
            return AgentOutput(
                success=False, content="",
                error=f"Privacy gate blocked: {exc}",
                duration_ms=duration_ms,
            )

        # Build tool schemas once
        openai_schemas    = get_tool_schemas_openai(self.allowed_tools)    if self.allowed_tools else []
        anthropic_schemas = get_tool_schemas_anthropic(self.allowed_tools) if self.allowed_tools else []

        messages = [Message(role="user", content=prompt)]
        total_tokens = 0

        for round_num in range(self.max_tool_rounds + 1):
            try:
                request = CompletionRequest(
                    messages        = messages,
                    system_prompt   = system,
                    max_tokens      = max_tokens,
                    task_id         = task.id,
                    correlation_id  = llm_correlation_id,
                    tools           = openai_schemas or None,
                    anthropic_tools = anthropic_schemas or None,
                )
                response = self.router.complete(request, task_type=self.task_type)
                total_tokens += response.tokens_total

            except Exception as e:
                duration_ms = int((time.monotonic() - start) * 1000)
                log.error(f"{self.agent_name} LLM call failed", error=str(e), task_id=task.id)
                # Consume the ledger entry so it doesn't linger
                if _gw:
                    try:
                        _gw.scrub_response("", llm_correlation_id, task.id)
                    except Exception:
                        pass
                return AgentOutput(success=False, content="", error=str(e), duration_ms=duration_ms)

            # ── No tool calls → final answer ──────────────────────────────
            if not response.wants_tool_call:
                duration_ms = int((time.monotonic() - start) * 1000)

                # ── Privacy gateway: inbound response scrub ───────────────
                clean_content = response.content
                if _gw:
                    try:
                        clean_content, _resp_result = _gw.scrub_response(
                            response       = response.content,
                            correlation_id = llm_correlation_id,
                            task_id        = task.id,
                        )
                    except Exception as exc:
                        log.warning("Privacy response scrub failed (non-fatal)",
                                    error=str(exc), task_id=task.id)

                return AgentOutput(
                    success=True,
                    content=clean_content,
                    tokens_used=total_tokens,
                    provider_used=response.provider,
                    duration_ms=duration_ms,
                )

            # ── Tool calls → execute and feed results back ─────────────────
            if round_num >= self.max_tool_rounds:
                log.warning(
                    "Tool loop hit max rounds — returning partial response",
                    agent=self.agent_name, rounds=round_num,
                )
                duration_ms = int((time.monotonic() - start) * 1000)
                # Scrub whatever partial content exists
                clean_content = response.content or "(max tool rounds reached)"
                if _gw:
                    try:
                        clean_content, _ = _gw.scrub_response(
                            clean_content, llm_correlation_id, task.id
                        )
                    except Exception:
                        pass
                return AgentOutput(
                    success=True,
                    content=clean_content,
                    tokens_used=total_tokens,
                    provider_used=response.provider,
                    duration_ms=duration_ms,
                    warnings=[f"Hit max_tool_rounds={self.max_tool_rounds}"],
                )

            # Append the assistant's tool-call turn to conversation history
            tool_call_summary = []
            for tc in response.tool_calls:
                tool_call_summary.append(f"Tool: {tc.name}({json.dumps(tc.arguments)})")
            messages.append(Message(
                role="assistant",
                content=response.content + "\n" + "\n".join(tool_call_summary),
            ))

            # Execute every tool the LLM requested
            tool_results_text = []
            for tc in response.tool_calls:
                metrics.inc("forge_tool_calls_total")
                result: ToolResult = execute_tool(tc.name, tc.arguments)
                status = "OK" if result.success else "ERROR"
                tool_results_text.append(
                    f"[Tool: {tc.name} → {status}]\n{result.output or result.error}"
                )
                if not result.success:
                    log.warning("Tool call failed", tool=tc.name, error=result.error)

            # Feed results back as a user message so the LLM can continue
            messages.append(Message(
                role="user",
                content="Tool results:\n\n" + "\n\n---\n\n".join(tool_results_text),
            ))

        # Should not be reached
        duration_ms = int((time.monotonic() - start) * 1000)
        return AgentOutput(success=False, content="", error="Tool loop exhausted",
                           duration_ms=duration_ms)

    def _parse_json(self, content: str) -> Optional[dict]:
        """Extract and parse JSON from LLM response."""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    @abstractmethod
    def run(self, task: Task) -> AgentOutput:
        ...


# ── Teal agents (generative) ──────────────────────────────────────────────────

class PlannerAgent(BaseAgent):
    agent_name = "planner"
    task_type = TaskType.PLANNING
    allowed_tools = ["web_search", "http_fetch", "git_clone", "list_files", "read_file", "search_files", "recall", "remember"]

    def run(self, task: Task) -> AgentOutput:
        # Planner only needs the raw description + slim context (no implementation yet)
        slim_ctx = _slim_context(task.context, exclude_keys=("implementation",))

        # ── Debugging mode: inject investigation report before planning ───────
        debug_section = ""
        if task.task_type.value == "debugging":
            inv = task.context.get("debug_investigation", {})
            if inv:
                root_cause    = inv.get("root_cause", "")
                files_change  = inv.get("files_to_change", [])
                files_read    = inv.get("files_read", [])
                theory        = inv.get("theory", "")
                regression    = inv.get("risk_of_regression", "")
                impl          = inv.get("implementation", "")
                debug_section = f"""
=== DEBUG INVESTIGATION REPORT (read before planning) ===
Root Cause: {root_cause}
Theory:     {theory}
Files Read: {", ".join(files_read[:10])}
Files to Change:
{chr(10).join(f"  - {f.get('file','?')} line {f.get('line','?')}: {f.get('change','?')}" for f in files_change[:10])}
Regression Risk: {regression}
Proposed Implementation:
{impl[:2000] if impl else "(none yet — planner must generate from above)"}
=== END DEBUG INVESTIGATION ===
"""
            else:
                debug_section = """
=== DEBUG MODE: No investigation report available ===
The DebuggerAgent did not produce a report. You MUST:
1. list_files on the target directory first
2. search_files for symptom strings
3. read_file on all matching files
4. Only then propose a plan
Do NOT guess at file names or line numbers.
===
"""

        # Inject re-plan feedback when this is a corrective pass
        replan_note = ""
        if task.context.get("replan_feedback"):
            replan_note = f"""
IMPORTANT — This is a corrective re-plan. The previous implementation failed:
{task.context["replan_feedback"]}

Your plan MUST address these failures. Do not repeat the same approach.
"""
        # Inject research findings when available
        research_note = ""
        if task.context.get("research_findings"):
            research_note = f"""
Research Findings (use these to inform your plan):
{task.context["research_findings"]}
"""

        prompt = f"""{debug_section}Analyse this requirement and produce a detailed task breakdown.

Requirement:
{task.description}
{replan_note}{research_note}
Context:
{slim_ctx}

Produce a JSON task breakdown. Each task must have clear, testable acceptance criteria."""

        output = self._call_llm(PLANNER_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
            if not structured:
                output.warnings.append("Could not parse structured JSON from planner output")
        return output


class ImplementerAgent(BaseAgent):
    agent_name = "implementer"
    task_type = TaskType.CODING
    allowed_tools = ["read_file", "write_file", "list_files", "search_files", "git_clone", "bash", "recall", "remember", "github_read", "generate_file"]

    def run(self, task: Task) -> AgentOutput:
        criteria_text = _format_criteria(task.acceptance_criteria)

        # Plan output can be large — trim it
        plan_raw = json.dumps(task.context.get("plan", {}), indent=2)
        plan_text = _trim(plan_raw, _PLAN_CHARS, label="plan")

        # Slim context: exclude plan (sent separately above) and implementation
        slim_ctx = _slim_context(task.context, exclude_keys=("implementation", "plan"))

        prompt = f"""Implement the following task. Produce production-grade code.

Task: {task.title}

Description:
{task.description}

Acceptance Criteria:
{criteria_text}

Plan:
{plan_text}

Additional Context:
{slim_ctx}

Requirements:
1. Full implementation — no placeholders
2. Type hints on all functions
3. Exception handling throughout
4. Structured logging at key decision points
5. Include verification steps at the end"""

        return self._call_llm(IMPLEMENTER_SYSTEM, prompt, task, max_tokens=8192)


class ReviewerAgent(BaseAgent):
    agent_name = "reviewer"
    task_type = TaskType.REVIEW
    allowed_tools = ["read_file", "list_files", "search_files", "recall", "remember"]

    def run(self, task: Task) -> AgentOutput:
        # Reviewer only needs: title, criteria, and the implementation (trimmed)
        raw_impl = task.context.get("implementation", task.description)
        implementation = _trim(raw_impl, _CONTEXT_CHARS, label="implementation")
        criteria_text = _format_criteria(task.acceptance_criteria)

        prompt = f"""Review this implementation. Be adversarial. Find every flaw.

Task: {task.title}

Acceptance Criteria:
{criteria_text}

Implementation:
{implementation}

Produce a structured JSON review verdict."""

        output = self._call_llm(REVIEWER_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
        return output


class QAAgent(BaseAgent):
    agent_name = "qa"
    task_type = TaskType.TESTING
    allowed_tools = ["read_file", "list_files", "bash"]

    def run(self, task: Task) -> AgentOutput:
        raw_impl      = task.context.get("implementation", "")
        implementation = _trim(raw_impl, _CONTEXT_CHARS, label="implementation")
        criteria_text  = _format_criteria(task.acceptance_criteria)

        # ── Test Intelligence: analyse the implementation before prompting ──
        intelligence_brief = ""
        try:
            from forge.intelligence import get_intelligence_engine
            engine  = get_intelligence_engine()
            report  = engine.analyse(raw_impl, filename=f"task_{task.id}.py")
            intelligence_brief = report.to_qa_brief()
            # Persist risk findings to cross-task memory
            try:
                store = get_memory_store()
                for entry in report.to_memory_entries(task_id=task.id):
                    store.remember(
                        key         = entry["key"],
                        value       = entry["value"],
                        memory_type = entry["memory_type"],
                        confidence  = entry["confidence"],
                        tags        = entry["tags"],
                        task_id     = entry["task_id"],
                    )
            except Exception:
                pass  # memory persistence is non-fatal
            # Store report summary in task context for Supervisor/audit
            task.context["test_intelligence"] = report.summary_dict()
        except Exception:
            pass  # intelligence is non-fatal — QA still runs without it

        prompt = f"""Verify these acceptance criteria are met. Generate targeted tests.

Task: {task.title}

Acceptance Criteria:
{criteria_text}

Implementation:
{implementation}

{intelligence_brief}

Instructions:
- Write tests in the priority order shown in the brief above
- Cover every critical path first, then risk areas, then untested areas
- Do NOT write tests for dead code locations listed in the brief
- For each criterion: state how to verify it, write the test, describe the failure mode
- Output structured JSON plus full test code."""

        output = self._call_llm(QA_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
        return output


# ── Coral agents (verification) ───────────────────────────────────────────────

class SecurityAgent(BaseAgent):
    agent_name = "security"
    task_type = TaskType.SECURITY
    allowed_tools = ["read_file", "list_files", "search_files"]

    SECRET_PATTERNS = [
        r"api_key\s*=\s*['\"][A-Za-z0-9+/]{20,}",
        r"password\s*=\s*['\"][^'\"]{8,}",
        r"secret\s*=\s*['\"][^'\"]{8,}",
        r"token\s*=\s*['\"][A-Za-z0-9+/\-_]{20,}",
        r"sk-[A-Za-z0-9]{32,}",
        r"sk-ant-[A-Za-z0-9\-]{20,}",
        r"AIza[A-Za-z0-9\-_]{35}",
    ]

    def _scan_for_secrets(self, content: str) -> List[str]:
        found = []
        for pattern in self.SECRET_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                found.append(f"Pattern matched: {pattern[:40]}...")
        return found

    def run(self, task: Task) -> AgentOutput:
        raw_impl = task.context.get("implementation", task.description)

        # Static scan runs on full implementation (fast, no LLM)
        secrets = self._scan_for_secrets(raw_impl)
        if secrets:
            metrics.inc("forge_hallucination_detections_total")
            log.warning("Secrets detected in implementation", task_id=task.id, count=len(secrets))

        # LLM prompt gets trimmed implementation
        implementation = _trim(raw_impl, _CONTEXT_CHARS, label="implementation")

        prompt = f"""Perform a security review of this implementation.

Task: {task.title}
Implementation:
{implementation}

Pre-scan detected: {secrets if secrets else 'No obvious secrets'}

Provide a thorough security analysis with structured JSON output."""

        output = self._call_llm(SECURITY_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            if structured is None:
                structured = {}
            structured["secrets_detected"] = secrets
            output.structured = structured
        return output


class DocumentationAgent(BaseAgent):
    agent_name = "documentation"
    task_type = TaskType.DOCUMENTATION

    def run(self, task: Task) -> AgentOutput:
        raw_impl = task.context.get("implementation", "")
        implementation = _trim(raw_impl, _CONTEXT_CHARS, label="implementation")

        prompt = f"""Generate comprehensive documentation for this implementation.

Task: {task.title}
Description: {task.description}

Implementation:
{implementation}

Produce:
1. Module/class docstrings (if code)
2. Usage examples
3. Configuration reference
4. An Architecture Decision Record (ADR) if significant design choices were made"""

        return self._call_llm(DOCUMENTATION_SYSTEM, prompt, task)


class RecoveryAgent(BaseAgent):
    agent_name = "recovery"
    task_type = TaskType.ANALYSIS

    def run(self, task: Task) -> AgentOutput:
        # Recovery only needs checkpoint data and recent audit — never the full impl
        checkpoint = task.checkpoint_data
        audit = task.audit_log[-5:] if task.audit_log else []

        prompt = f"""Analyse this failed/blocked task and propose a recovery plan.

Task: {task.title}
Current State: {task.state.value}
Last Checkpoint: {json.dumps(checkpoint, indent=2)}
Recent Audit: {json.dumps(audit, indent=2)}

Determine:
1. What checkpoint to resume from
2. Whether partial work is salvageable
3. A step-by-step recovery plan
4. Risk of data loss

Output structured JSON."""

        output = self._call_llm(RECOVERY_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
        return output


class PerformanceAgent(BaseAgent):
    agent_name = "performance"
    task_type = TaskType.ANALYSIS

    def run(self, task: Task) -> AgentOutput:
        raw_impl = task.context.get("implementation", task.description)
        implementation = _trim(raw_impl, _CONTEXT_CHARS, label="implementation")

        prompt = f"""Analyse the performance characteristics of this implementation.

Task: {task.title}
Implementation:
{implementation}

Identify bottlenecks, complexity issues, and optimisation opportunities.
Output structured JSON with findings and recommendations."""

        output = self._call_llm(PERFORMANCE_SYSTEM, prompt, task)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
        return output


RESEARCH_SYSTEM = """You are FORGE Researcher — an autonomous retrieval and knowledge acquisition agent.

Your responsibility is NOT to answer questions directly.
Your responsibility is to DISCOVER, RETRIEVE, VALIDATE, and SYNTHESIZE knowledge.

Retrieval loop:
1. Understand the objective
2. Plan what information is needed
3. Use web_search and http_fetch to retrieve from multiple independent sources
4. Evaluate credibility and detect conflicts between sources
5. Fill knowledge gaps with additional searches
6. Synthesize findings with confidence levels

Rules:
- Never fabricate statistics, laws, quotes, or URLs
- Every important claim needs at least 2 independent sources
- If sources conflict, document both positions
- State "Evidence not found" rather than guessing
- Keep searching until confident or budget exhausted (max 8 tool rounds)

Output format:
{
  "objective": "what was researched",
  "sources_consulted": ["url1", "url2", ...],
  "findings": [{"claim": "...", "confidence": "HIGH|MEDIUM|LOW", "sources": [...]}],
  "conflicts": [{"claim": "...", "source_a": "...", "source_b": "...", "resolution": "..."}],
  "gaps": ["things still unknown"],
  "summary": "synthesized paragraph",
  "overall_confidence": "HIGH|MEDIUM|LOW"
}"""


class ResearchAgent(BaseAgent):
    """
    Autonomous retrieval agent. Uses web_search, http_fetch, and git_clone
    to build a verified knowledge model before answering.

    Runs up to max_tool_rounds of retrieval, analysis, and gap-filling
    before producing a structured findings report.
    """
    agent_name   = "researcher"
    task_type    = TaskType.ANALYSIS
    allowed_tools = ["web_search", "http_fetch", "git_clone", "read_file", "list_files", "search_files", "remember", "recall"]
    max_tool_rounds = 12   # research needs more rounds than coding

    def run(self, task: Task) -> AgentOutput:
        slim_ctx = _slim_context(task.context)

        # ── Repository Intelligence: if task references a local path or
        #    a cloned repo, build an AS-IS prompt before web research ─────
        as_is_section = ""
        repo_path = (
            task.context.get("repo_path")
            or task.context.get("repository_path")
            or task.context.get("codebase_path")
        )
        if repo_path and os.path.isdir(repo_path):
            try:
                from forge.intelligence.repository import get_repo_engine
                ri_engine = get_repo_engine()
                ri_report = ri_engine.analyse(repo_path)
                as_is_prompt = ri_report.to_as_is_prompt()
                as_is_section = f"\n\nCODEBASE AS-IS (reverse-engineered):\n{as_is_prompt}\n"
                task.context["repository_as_is"] = ri_report.summary_dict()
                task.context["repository_as_is_prompt"] = as_is_prompt
                # Persist to cross-task memory so Planner can use it
                try:
                    store = get_memory_store()
                    store.remember(
                        key         = f"repo_as_is:{ri_report.repo_name}",
                        value       = as_is_prompt[:1500],
                        memory_type = "fact",
                        confidence  = 0.9,
                        tags        = ["repository", "as_is", ri_report.repo_name],
                        task_id     = task.id,
                    )
                except Exception:
                    pass
            except Exception as exc:
                as_is_section = f"\n\n[Repository intelligence failed: {exc}]\n"

        prompt = f"""Research this objective thoroughly using available tools.

Objective: {task.description}

Context:
{slim_ctx}{as_is_section}

Instructions:
1. Start with 2-3 web searches to understand the landscape
2. Fetch primary sources (official docs, repos, papers) directly
3. Cross-verify key claims across independent sources
4. Identify and document any conflicts between sources
5. Note any gaps in your knowledge
6. Synthesize everything into a structured findings report

If a CODEBASE AS-IS section is present above, use it as authoritative
ground truth about the existing architecture — your research should extend
and validate it, not replace it.

Use as many tool calls as needed (up to {self.max_tool_rounds} rounds).
Do not stop after one search — research deeply."""

        output = self._call_llm(RESEARCH_SYSTEM, prompt, task, max_tokens=2048)
        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured
            if not structured:
                output.structured = {"summary": output.content, "overall_confidence": "MEDIUM"}
        return output


# ── Agent registry ────────────────────────────────────────────────────────────



DEBUGGER_SYSTEM = """You are FORGE Debugger — a senior debugging engineer modelled on the Codex/Claude Code investigation approach.

Your mandate: investigate before you touch anything.

INVESTIGATION PROTOCOL (mandatory — do not skip any step):

Phase 1 — MAP (always first)
  1. list_files on the target directory recursively
  2. read_file on key entry points: package.json / pyproject.toml / Cargo.toml / go.mod
  3. search_files for the exact symptom string from the bug report
  4. read_file on every file that contains the symptom

Phase 2 — UNDERSTAND
  5. Trace the data flow: where does the value originate? Where is it overwritten?
  6. Locate hardcoded values, fallbacks, default state — the bug is almost always here
  7. read_file on every file in the call chain between origin and symptom
  8. Form a precise theory: "File X line N overwrites Y with hardcoded Z because..."

Phase 3 — PLAN (only after Phases 1 and 2 are complete)
  9. State which files change, which lines, and why
  10. State what will NOT change and why
  11. State what might break and how you will verify it does not
  12. State your theory as a falsifiable hypothesis

Phase 4 — IMPLEMENT (targeted, minimal)
  13. Make only the changes required by the theory
  14. No refactoring, no cleanup, no unrelated improvements
  15. One logical change per file

OUTPUT FORMAT:
{
  "investigation_complete": true,
  "files_read": ["list of every file you read"],
  "symptom_location": "file:line where bug manifests",
  "root_cause": "exact explanation: file, line, variable, value",
  "theory": "falsifiable hypothesis",
  "files_to_change": [{"file": "...", "line": 0, "change": "..."}],
  "files_unchanged": ["... and why"],
  "risk_of_regression": "what else might break",
  "implementation": "the actual code changes"
}

NEVER plan without reading. NEVER assume file contents. NEVER guess at line numbers."""


class DebuggerAgent(BaseAgent):
    """
    Investigation-first debugging agent.

    Mirrors the Codex/Claude Code debugging pattern from how_debugging_can_be_done.txt:
      1. Map the project (list_files, search_files) — 3 tool calls
      2. Read every relevant file (read_file — no guessing) — 4 tool calls
      3. Form a theory with file + line + variable precision
      4. Make the minimal targeted change

    Runs as the RESEARCH phase for --type debugging tasks so the Planner
    receives a complete investigation report before writing a single line.
    """

    agent_name      = "debugger"
    task_type       = TaskType.DEBUGGING
    allowed_tools   = [
        "read_file", "list_files", "search_files",
        "bash", "git_clone", "recall",
    ]
    max_tool_rounds = 20

    def run(self, task: Task) -> AgentOutput:
        import os
        target = (
            task.context.get("repo_path")
            or task.context.get("repository_path")
            or task.context.get("codebase_path")
            or task.context.get("target_dir")
            or os.environ.get("FORGE_WORKSPACE", "workspace")
        )

        prompt = f"""You are investigating a bug. Follow the INVESTIGATION PROTOCOL exactly.

TARGET DIRECTORY: {target}

BUG REPORT:
{task.description}

SYMPTOM STRINGS TO SEARCH FOR:
{task.context.get("symptoms") or task.context.get("search_strings") or "(search for key identifiers from the bug report)"}

ACCEPTANCE CRITERIA:
{chr(10).join(f"- {{c}}" for c in (task.acceptance_criteria or [])) or "- Bug no longer reproduces after fix"}

BEGIN NOW:
Step 1: list_files on {target}
Step 2: search_files for symptom strings
Step 3: read_file on every matching file
Step 4: form your theory
Step 5: output the structured JSON report

Do not write any code until you have completed Phase 1 and Phase 2."""

        output = self._call_llm(DEBUGGER_SYSTEM, prompt, task, max_tokens=4096)

        if output.success:
            structured = self._parse_json(output.content)
            output.structured = structured or {"raw_investigation": output.content}
            if structured:
                task.context["debug_investigation"]    = structured
                task.context["root_cause"]             = structured.get("root_cause", "")
                task.context["files_to_change"]        = structured.get("files_to_change", [])
                task.context["investigation_complete"] = structured.get("investigation_complete", False)
                task.context["files_read"]             = structured.get("files_read", [])

        return output

def build_agents(router: ModelRouter) -> Dict[str, BaseAgent]:
    return {
        "planner":       PlannerAgent(router),
        "implementer":   ImplementerAgent(router),
        "reviewer":      ReviewerAgent(router),
        "qa":            QAAgent(router),
        "security":      SecurityAgent(router),
        "documentation": DocumentationAgent(router),
        "recovery":      RecoveryAgent(router),
        "performance":   PerformanceAgent(router),
        "researcher":    ResearchAgent(router),
        "debugger":      DebuggerAgent(router),
    }