"""
FORGE Distilled Self-Tutor
===========================
The top-level orchestrator. Assembles evidence, runs extractors,
updates the principle store, and exposes the public API.

This is both:
  1. A standalone system — `forge tutor` CLI for offline analysis
  2. A Forge component — hooked into the Supervisor post-task loop

Public API::

    from forge.tutor import SelfTutor, get_tutor

    # After a task completes
    tutor = get_tutor()
    tutor.learn_from_task(task, result, ctx)

    # From an external repository
    tutor.learn_from_repo("/path/to/repo")

    # From a specification file
    tutor.learn_from_spec("requirements.md")

    # From a raw execution trace
    tutor.learn_from_trace(trace_text, task_id="TASK-001")

    # Inject principles into agent system prompt
    enriched_system = tutor.enrich_prompt(system_prompt, agent_name="planner")

    # Report
    print(tutor.report())
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from forge.telemetry.logger import get_logger
from forge.tutor.evidence import (
    Evidence,
    evidence_from_trace,
    evidence_from_spec,
    evidence_from_repo_report,
    evidence_from_test_report,
    evidence_from_task_result,
)
from forge.tutor.principles import PrincipleEngine, Principle
from forge.tutor.store import PrincipleStore, get_principle_store

log = get_logger("tutor")

# Categories relevant to each agent — used to filter principle injection
_AGENT_CATEGORIES = {
    "planner":       ["planning", "decomposition", "architecture"],
    "implementer":   ["architecture", "security", "observability"],
    "reviewer":      ["verification", "security", "architecture"],
    "qa":            ["testing", "verification"],
    "security":      ["security", "verification"],
    "recovery":      ["error_recovery", "planning"],
    "researcher":    ["planning", "architecture"],
    "documentation": ["architecture", "decomposition"],
    "performance":   ["architecture", "testing"],
}


class SelfTutor:
    """
    The FORGE Distilled Self-Tutor.

    Inputs any combination of:
      - execution traces
      - repository scans
      - specification documents
      - task outcomes
      - external reference repos

    Extracts reusable engineering principles and maintains a
    confidence-weighted principle store.

    Principles are injected into agent system prompts via enrich_prompt(),
    making every new task benefit from everything Forge has learned.
    """

    def __init__(self, db_path: str = "forge_tasks.db"):
        self._engine = PrincipleEngine()
        self._store  = get_principle_store(db_path)

    # ── Learn from sources ─────────────────────────────────────────────────

    def learn_from_task(
        self,
        task,         # forge.orchestration.task.Task
        result,       # forge.orchestration.task.TaskResult
        ctx=None,     # forge.orchestration.engine.ExecutionContext (optional)
    ) -> List[Principle]:
        """
        Called by the Supervisor after every task completion.
        Non-fatal: any failure is logged but does not affect task outcome.
        """
        try:
            evidence = evidence_from_task_result(task, result, ctx)
            return self._process(evidence)
        except Exception as exc:
            log.warning("Tutor: learn_from_task failed (non-fatal)", error=str(exc))
            return []

    def learn_from_trace(
        self,
        trace_text: str,
        task_id: str = "",
    ) -> List[Principle]:
        """Parse a raw JSON execution trace and extract principles."""
        try:
            evidence = evidence_from_trace(trace_text, task_id)
            return self._process(evidence)
        except Exception as exc:
            log.warning("Tutor: learn_from_trace failed", error=str(exc))
            return []

    def learn_from_spec(self, path: str) -> List[Principle]:
        """Extract principles from a specification / ADR document."""
        try:
            evidence = evidence_from_spec(path)
            return self._process(evidence)
        except Exception as exc:
            log.warning("Tutor: learn_from_spec failed", error=str(exc))
            return []

    def learn_from_repo(
        self,
        repo_path: str,
        repo_name: Optional[str] = None,
        is_external: bool = False,
    ) -> List[Principle]:
        """
        Run Repository Intelligence on a directory, then extract principles.

        is_external=True reduces quality weight slightly (external repos
        may use different conventions than what Forge targets).
        """
        try:
            from forge.intelligence.repository import get_repo_engine
            report = get_repo_engine().analyse(repo_path, repo_name=repo_name)
            evidence = evidence_from_repo_report(report)
            if is_external:
                evidence.quality *= 0.85
                evidence.source_type = "external_repo"
            return self._process(evidence)
        except Exception as exc:
            log.warning("Tutor: learn_from_repo failed", error=str(exc))
            return []

    def learn_from_repo_batch(
        self,
        repos: List[dict],
    ) -> List[Principle]:
        """
        Process multiple repos together.
        Cross-repo confirmation: same principle from independent repos
        gets a confidence boost.

        repos: list of {"path": str, "name": str (optional),
                         "is_external": bool (optional)}
        """
        all_evidence = []
        for spec in repos:
            path = spec.get("path", "")
            if not path or not Path(path).exists():
                log.warning("Tutor: repo path not found, skipping", path=path)
                continue
            try:
                from forge.intelligence.repository import get_repo_engine
                report   = get_repo_engine().analyse(
                    path,
                    repo_name=spec.get("name"),
                )
                evidence = evidence_from_repo_report(report)
                if spec.get("is_external", False):
                    evidence.quality *= 0.85
                    evidence.source_type = "external_repo"
                all_evidence.append(evidence)
            except Exception as exc:
                log.warning("Tutor: learn_from_repo_batch item failed",
                            path=path, error=str(exc))

        if not all_evidence:
            return []

        # Use batch processing for cross-source confirmation
        updates = self._engine.process_batch(all_evidence)
        updated = []
        for principle, action in updates:
            try:
                p = self._store.upsert(principle, action)
                updated.append(p)
            except Exception:
                pass

        log.info("Tutor: batch repo learning complete",
                 repos=len(all_evidence),
                 principles_updated=len(updated))
        return updated

    def learn_from_spec_dir(self, directory: str) -> List[Principle]:
        """
        Scan a directory for specification files and learn from all of them.
        Recognises: requirements.md, design.md, tasks.md, CLAUDE.md,
                    ADR-*.md, *.spec.md, architecture.md
        """
        root = Path(directory)
        if not root.exists():
            return []

        spec_patterns = [
            "requirements.md", "design.md", "tasks.md", "CLAUDE.md",
            "architecture.md", "ARCHITECTURE.md",
        ]
        found = []
        for pat in spec_patterns:
            p = root / pat
            if p.exists():
                found.append(str(p))
        found += [str(f) for f in root.rglob("ADR-*.md")]
        found += [str(f) for f in root.rglob("*.spec.md")]

        all_updated = []
        all_evidence = [evidence_from_spec(f) for f in found]
        if not all_evidence:
            return []

        updates = self._engine.process_batch(all_evidence)
        for principle, action in updates:
            try:
                all_updated.append(self._store.upsert(principle, action))
            except Exception:
                pass

        log.info("Tutor: spec dir learning complete",
                 files=len(found),
                 principles=len(all_updated))
        return all_updated

    # ── Prompt enrichment ──────────────────────────────────────────────────

    def enrich_prompt(
        self,
        system_prompt: str,
        agent_name: str = "",
        max_principles: int = 6,
    ) -> str:
        """
        Inject relevant principles into an agent's system prompt.

        Uses agent_name to filter for the most relevant categories.
        Only active, above-threshold principles are injected.
        """
        categories = _AGENT_CATEGORIES.get(agent_name, None)
        block = self._store.to_prompt_block(
            categories      = categories,
            max_principles  = max_principles,
        )
        if not block:
            return system_prompt
        return f"{system_prompt}\n\n{block}"

    # ── Reporting ──────────────────────────────────────────────────────────

    def report(self, category: Optional[str] = None) -> str:
        """Human-readable report of all known principles."""
        principles = self._store.list_active(category=category)
        stats      = self._store.stats()

        lines = [
            "",
            "  ── FORGE Self-Tutor: Engineering Principles ──────────────────",
            f"  Total: {stats['total']}  Active: {stats['active']}  "
            f"Core: {stats['core']}  Mean confidence: {stats['mean_confidence']:.0%}",
            "",
        ]

        # Group by category
        by_cat: dict = {}
        for p in principles:
            by_cat.setdefault(p.category, []).append(p)

        for cat, ps in sorted(by_cat.items()):
            lines.append(f"  ── {cat.upper()} ─────────────────────────────────────────")
            for p in sorted(ps, key=lambda x: x.confidence, reverse=True):
                core_marker = " ★" if p.core else ""
                lines.append(
                    f"  [{p.confidence:.0%}]{core_marker} {p.statement}"
                )
                lines.append(
                    f"      sources={p.source_count}  "
                    f"confirmations={p.confirmations}  "
                    f"contradictions={p.contradictions}"
                )
                lines.append(f"      {p.rationale[:120]}")
                lines.append("")

        if not principles:
            lines.append("  No principles yet. Run: forge tutor learn --repo ./  "
                         "or complete some tasks.")

        return "\n".join(lines)

    def stats(self) -> dict:
        return self._store.stats()

    # ── Internal ──────────────────────────────────────────────────────────

    def _process(self, evidence: Evidence) -> List[Principle]:
        updates = self._engine.process(evidence)
        updated = []
        for principle, action in updates:
            try:
                p = self._store.upsert(principle, action)
                updated.append(p)
            except Exception as exc:
                log.warning("Tutor: store upsert failed", error=str(exc))
        if updates:
            log.info("Tutor: learning complete",
                     source_type=evidence.source_type,
                     source_id=evidence.source_id,
                     principles_updated=len(updated))
        return updated
