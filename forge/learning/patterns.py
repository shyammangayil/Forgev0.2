"""
FORGE Knowledge Extraction & Learning
======================================
Analyses the audit trail from completed and failed tasks to extract:

  1. FailurePatternDetector  — recurring failure patterns + auto-fix suggestions
  2. BestPracticesExtractor  — successful patterns → project-specific guidelines
  3. ModelCapabilityProfiler — empirical provider/model performance tracking
  4. TaskPlaybookGenerator   — generate reusable playbooks from completed tasks

All analysis reads from forge_tasks.db — no LLM calls.
Results are stored back into the memory store so agents can use them.

CLI: forge learn [analyse|report|playbooks|profile]
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.telemetry.logger import get_logger

log = get_logger("learning")


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FailurePattern:
    pattern_id:   str
    phase:        str
    error_type:   str
    occurrences:  int
    examples:     List[str]
    suggested_fix: str
    confidence:   float  # 0.0 - 1.0


@dataclass
class BestPractice:
    practice_id: str
    category:    str
    description: str
    evidence:    List[str]  # task IDs where this was observed
    confidence:  float


@dataclass
class ModelProfile:
    provider:     str
    model:        str
    task_type:    str
    success_rate: float
    avg_duration_s: float
    avg_tokens:   int
    avg_cost_usd: float
    sample_size:  int


@dataclass
class Playbook:
    playbook_id:  str
    task_type:    str
    title:        str
    description:  str
    steps:        List[str]
    tips:         List[str]
    based_on:     List[str]  # task IDs


# ── Database reader ───────────────────────────────────────────────────────────

class TaskDataReader:
    """Reads raw task data from SQLite for analysis."""

    def __init__(self, db_path: str = "forge_tasks.db"):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def load_all_tasks(self) -> List[dict]:
        """Load all tasks with their full context."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, title, task_type, state, context, result, audit_log FROM tasks"
            ).fetchall()
            tasks = []
            for row in rows:
                t = dict(row)
                for field in ("context", "result", "audit_log"):
                    if t.get(field) and isinstance(t[field], str):
                        try:
                            t[field] = json.loads(t[field])
                        except Exception:
                            t[field] = {}
                tasks.append(t)
            return tasks
        finally:
            conn.close()

    def load_token_usage(self) -> List[dict]:
        """Load token usage records if available."""
        conn = self._conn()
        try:
            # Check if token_usage table exists
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                "SELECT * FROM token_usage ORDER BY recorded_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def load_checkpoints(self) -> List[dict]:
        """Load checkpoint data for phase timing analysis."""
        conn = self._conn()
        try:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute("SELECT * FROM checkpoints").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── 1. Failure Pattern Detector ───────────────────────────────────────────────

class FailurePatternDetector:
    """
    Analyses failed tasks to identify recurring patterns.
    Groups failures by phase + error type, then generates fix suggestions.
    """

    # Error type → suggested fix
    KNOWN_FIXES: Dict[str, str] = {
        "timed out":                "Increase FORGE_OLLAMA_TIMEOUT. Reduce FORGE_CONTEXT_CHARS.",
        "Authentication failed":    "Check API key in .env. Verify FORGE_FALLBACK_CHAIN matches available keys.",
        "Context length exceeded":  "Reduce FORGE_CONTEXT_CHARS and FORGE_PLAN_CHARS.",
        "All providers exhausted":  "Ensure at least one provider key is valid. Check FORGE_FALLBACK_CHAIN.",
        "placeholder":              "Implementation contained TODO/pass placeholders. Try a more powerful model.",
        "REJECTED":                 "Review rejected implementation. Consider re-planning with more specific criteria.",
        "CRITICAL":                 "Security gate failed. Audit generated code for SQL injection or hardcoded secrets.",
        "rate limit":               "Provider rate limit hit. Add delay between tasks or switch providers.",
        "JSON":                     "Agent returned malformed JSON. Reduce max_tokens or simplify the prompt.",
    }

    def analyse(self, tasks: List[dict]) -> List[FailurePattern]:
        failed_tasks = [t for t in tasks if t.get("state") == "FAILED"]
        if not failed_tasks:
            return []

        # Group by (phase, error_type)
        buckets: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for task in failed_tasks:
            result = task.get("result") or {}
            error  = str(result.get("error", "unknown"))

            # Extract phase from error message
            phase = "UNKNOWN"
            for p in ("PLAN", "EXECUTE", "VERIFY", "REVIEW", "TEST", "COMMIT"):
                if p in error:
                    phase = p
                    break

            # Classify error type
            error_type = "unknown"
            for key in self.KNOWN_FIXES:
                if key.lower() in error.lower():
                    error_type = key
                    break

            bucket_key = (phase, error_type)
            buckets[bucket_key].append(task["id"])

        patterns = []
        for (phase, error_type), task_ids in buckets.items():
            if len(task_ids) < 1:
                continue
            fix = self.KNOWN_FIXES.get(error_type,
                  "Review logs for this error pattern and adjust configuration.")
            confidence = min(1.0, 0.5 + (len(task_ids) - 1) * 0.1)
            patterns.append(FailurePattern(
                pattern_id=f"fail_{phase}_{error_type[:20].replace(' ', '_')}",
                phase=phase,
                error_type=error_type,
                occurrences=len(task_ids),
                examples=task_ids[:3],
                suggested_fix=fix,
                confidence=confidence,
            ))

        return sorted(patterns, key=lambda p: p.occurrences, reverse=True)

    def format_report(self, patterns: List[FailurePattern]) -> str:
        if not patterns:
            return "  No recurring failure patterns detected."
        lines = [f"  {len(patterns)} failure pattern(s) detected:\n"]
        for p in patterns:
            lines += [
                f"  Pattern: {p.pattern_id}",
                f"    Phase:       {p.phase}",
                f"    Error type:  {p.error_type}",
                f"    Occurrences: {p.occurrences} (confidence: {p.confidence:.0%})",
                f"    Fix:         {p.suggested_fix}",
                f"    Examples:    {', '.join(p.examples[:3])}",
                "",
            ]
        return "\n".join(lines)


# ── 2. Best Practices Extractor ───────────────────────────────────────────────

class BestPracticesExtractor:
    """
    Identifies patterns from successfully completed tasks and extracts
    project-specific best practices that can guide future tasks.
    """

    def extract(self, tasks: List[dict]) -> List[BestPractice]:
        completed = [t for t in tasks if t.get("state") == "COMPLETE"]
        practices = []

        # Practice 1: Commonly used task types
        type_counts: Dict[str, List[str]] = defaultdict(list)
        for t in completed:
            type_counts[t.get("task_type", "coding")].append(t["id"])
        for task_type, task_ids in type_counts.items():
            if len(task_ids) >= 2:
                practices.append(BestPractice(
                    practice_id=f"preferred_type_{task_type}",
                    category="task_routing",
                    description=f"This project frequently uses '{task_type}' tasks ({len(task_ids)}x)",
                    evidence=task_ids[:3],
                    confidence=min(1.0, len(task_ids) / 5),
                ))

        # Practice 2: Extract common acceptance criteria patterns
        criteria_words: Dict[str, int] = defaultdict(int)
        for t in completed:
            context = t.get("context") or {}
            criteria = context.get("acceptance_criteria", [])
            if isinstance(criteria, list):
                for c in criteria:
                    for word in str(c).lower().split():
                        if len(word) > 5:
                            criteria_words[word] += 1

        common_criteria = [(w, c) for w, c in criteria_words.items() if c >= 2]
        common_criteria.sort(key=lambda x: x[1], reverse=True)
        if common_criteria[:5]:
            practices.append(BestPractice(
                practice_id="common_criteria_terms",
                category="quality_standards",
                description=(
                    "Frequently used acceptance criteria terms: "
                    + ", ".join(w for w, _ in common_criteria[:5])
                ),
                evidence=[t["id"] for t in completed[:3]],
                confidence=0.7,
            ))

        # Practice 3: Provider preference (which provider completed most tasks)
        provider_completions: Dict[str, int] = defaultdict(int)
        for t in completed:
            result = t.get("result") or {}
            provider = result.get("provider_used", "")
            if provider:
                provider_completions[provider] += 1
        if provider_completions:
            best = max(provider_completions, key=provider_completions.get)
            practices.append(BestPractice(
                practice_id=f"preferred_provider_{best}",
                category="provider_selection",
                description=(
                    f"'{best}' has completed the most tasks successfully "
                    f"({provider_completions[best]} completions)"
                ),
                evidence=[],
                confidence=0.8,
            ))

        return practices

    def format_report(self, practices: List[BestPractice]) -> str:
        if not practices:
            return "  No best practices extracted yet (need more completed tasks)."
        lines = [f"  {len(practices)} best practice(s) identified:\n"]
        for p in practices:
            lines += [
                f"  [{p.category}] {p.description}",
                f"    Confidence: {p.confidence:.0%}",
                "",
            ]
        return "\n".join(lines)


# ── 3. Model Capability Profiler ──────────────────────────────────────────────

class ModelCapabilityProfiler:
    """
    Builds an empirical performance profile for each provider/model combination.
    Tracks success rate, latency, token usage, and cost by task type.
    Generates routing recommendations based on historical performance.
    """

    def profile(self, tasks: List[dict], usage_records: List[dict]) -> List[ModelProfile]:
        # Group usage records by (provider, model, task_type)
        buckets: Dict[Tuple, List[dict]] = defaultdict(list)

        for record in usage_records:
            key = (
                record.get("provider", ""),
                record.get("model", ""),
                record.get("phase", ""),
            )
            buckets[key].append(record)

        # Also analyse task-level data
        task_by_id = {t["id"]: t for t in tasks}

        profiles = []
        for (provider, model, phase), records in buckets.items():
            if not provider or not model:
                continue

            durations = [r.get("duration_ms", 0) / 1000 for r in records]
            tokens    = [r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in records]
            costs     = [r.get("cost_usd", 0) for r in records]

            # Rough success rate from tasks that used this provider
            task_ids_used = set(r.get("task_id") for r in records)
            completed = sum(
                1 for tid in task_ids_used
                if task_by_id.get(tid, {}).get("state") == "COMPLETE"
            )
            success_rate = completed / len(task_ids_used) if task_ids_used else 0.0

            profiles.append(ModelProfile(
                provider=provider,
                model=model,
                task_type=phase,
                success_rate=success_rate,
                avg_duration_s=statistics.mean(durations) if durations else 0,
                avg_tokens=int(statistics.mean(tokens)) if tokens else 0,
                avg_cost_usd=statistics.mean(costs) if costs else 0.0,
                sample_size=len(records),
            ))

        return sorted(profiles, key=lambda p: (p.provider, p.success_rate), reverse=True)

    def routing_recommendations(self, profiles: List[ModelProfile]) -> List[str]:
        """Generate plain-text routing recommendations from profiles."""
        recs = []
        by_provider: Dict[str, List[ModelProfile]] = defaultdict(list)
        for p in profiles:
            by_provider[p.provider].append(p)

        for provider, profs in by_provider.items():
            avg_success = statistics.mean(p.success_rate for p in profs)
            avg_cost    = statistics.mean(p.avg_cost_usd for p in profs)

            if avg_success >= 0.9:
                recs.append(f"✅ {provider}: High success rate ({avg_success:.0%}) — reliable choice")
            elif avg_success < 0.5:
                recs.append(f"⚠️  {provider}: Low success rate ({avg_success:.0%}) — use as fallback only")

            if avg_cost == 0:
                recs.append(f"💰 {provider}: Free (local) — prefer for high-volume tasks")
            elif avg_cost > 0.10:
                recs.append(f"💸 {provider}: High cost (avg ${avg_cost:.4f}/call) — use for complex tasks only")

        return recs

    def format_report(self, profiles: List[ModelProfile]) -> str:
        if not profiles:
            return "  No model performance data yet (token_usage table empty)."
        lines = [f"  Model performance ({len(profiles)} profiles):\n"]
        lines.append(f"  {'Provider':<12} {'Model':<30} {'Success':<8} {'Avg dur':<10} {'Avg cost':<10} {'Samples'}")
        lines.append("  " + "-" * 80)
        for p in profiles[:20]:
            lines.append(
                f"  {p.provider:<12} {p.model:<30} "
                f"{p.success_rate:.0%}    "
                f"{p.avg_duration_s:>6.1f}s   "
                f"${p.avg_cost_usd:.6f}  "
                f"{p.sample_size}"
            )
        return "\n".join(lines)


# ── 4. Playbook Generator ─────────────────────────────────────────────────────

class TaskPlaybookGenerator:
    """
    Generates reusable playbooks from successfully completed tasks.
    Playbooks capture: what worked, which acceptance criteria were effective,
    and tips for similar future tasks.
    """

    def generate(self, tasks: List[dict]) -> List[Playbook]:
        completed = [t for t in tasks if t.get("state") == "COMPLETE"]
        playbooks = []

        by_type: Dict[str, List[dict]] = defaultdict(list)
        for t in completed:
            by_type[t.get("task_type", "coding")].append(t)

        for task_type, type_tasks in by_type.items():
            if not type_tasks:
                continue

            # Collect successful acceptance criteria as steps
            all_criteria = []
            for t in type_tasks:
                ctx = t.get("context") or {}
                criteria = ctx.get("acceptance_criteria", [])
                if isinstance(criteria, list):
                    all_criteria.extend(str(c) for c in criteria)

            # Deduplicate
            seen = set()
            unique_criteria = []
            for c in all_criteria:
                key = c.lower()[:40]
                if key not in seen:
                    seen.add(key)
                    unique_criteria.append(c)

            # Build tips from common patterns
            tips = []
            providers = [
                (t.get("result") or {}).get("provider_used", "")
                for t in type_tasks if t.get("result")
            ]
            if providers:
                most_common = max(set(p for p in providers if p),
                                  key=providers.count, default="")
                if most_common:
                    tips.append(f"Provider '{most_common}' completed similar tasks successfully")

            avg_criteria = len(all_criteria) / max(len(type_tasks), 1)
            if avg_criteria > 0:
                tips.append(
                    f"Similar tasks used ~{avg_criteria:.0f} acceptance criteria on average"
                )

            playbooks.append(Playbook(
                playbook_id=f"playbook_{task_type}",
                task_type=task_type,
                title=f"{task_type.title()} Task Playbook",
                description=(
                    f"Generated from {len(type_tasks)} completed {task_type} task(s). "
                    f"Captures effective patterns for this project."
                ),
                steps=unique_criteria[:10],
                tips=tips,
                based_on=[t["id"] for t in type_tasks[:5]],
            ))

        return playbooks

    def format_report(self, playbooks: List[Playbook]) -> str:
        if not playbooks:
            return "  No playbooks generated yet (need completed tasks)."
        lines = [f"  {len(playbooks)} playbook(s) generated:\n"]
        for pb in playbooks:
            lines += [
                f"  📋 {pb.title}",
                f"     {pb.description}",
            ]
            if pb.steps:
                lines.append("     Steps (from successful tasks):")
                for s in pb.steps[:5]:
                    lines.append(f"       - {s[:80]}")
            if pb.tips:
                lines.append("     Tips:")
                for t in pb.tips:
                    lines.append(f"       💡 {t}")
            lines.append("")
        return "\n".join(lines)


# ── Main learning engine ───────────────────────────────────────────────────────

class LearningEngine:
    """
    Orchestrates all learning subsystems and persists findings to the memory store.
    """

    def __init__(self, db_path: str = "forge_tasks.db"):
        self.db_path = db_path
        self.reader  = TaskDataReader(db_path)

    def run_full_analysis(self, persist: bool = True) -> dict:
        """
        Run all analysis and optionally persist findings to memory store.
        Returns a summary dict with all findings.
        """
        log.info("Starting learning analysis", db=self.db_path)

        try:
            tasks   = self.reader.load_all_tasks()
            usage   = self.reader.load_token_usage()
        except FileNotFoundError as e:
            return {"error": str(e), "note": "Run at least one task first"}

        # Run all analysers
        failures  = FailurePatternDetector().analyse(tasks)
        practices = BestPracticesExtractor().extract(tasks)
        profiles  = ModelCapabilityProfiler().profile(tasks, usage)
        playbooks = TaskPlaybookGenerator().generate(tasks)

        result = {
            "tasks_analysed":    len(tasks),
            "failure_patterns":  len(failures),
            "best_practices":    len(practices),
            "model_profiles":    len(profiles),
            "playbooks":         len(playbooks),
            "data": {
                "failures":  [vars(f) for f in failures],
                "practices": [vars(p) for p in practices],
                "profiles":  [vars(p) for p in profiles],
                "playbooks": [vars(p) for p in playbooks],
            },
        }

        # Persist to memory store so agents can use findings
        if persist:
            self._persist_to_memory(failures, practices, profiles)

        log.info("Learning analysis complete", **{k: v for k, v in result.items() if k != "data"})
        return result

    def _persist_to_memory(
        self,
        failures:  List[FailurePattern],
        practices: List[BestPractice],
        profiles:  List[ModelProfile],
    ) -> None:
        """Write findings to the cross-task memory store."""
        try:
            from forge.memory.store import get_memory_store
            store = get_memory_store(self.db_path)

            for f in failures[:5]:  # top 5 failure patterns
                store.remember(
                    "error",
                    f"failure_pattern:{f.pattern_id}",
                    f"Recurring failure in {f.phase}: {f.error_type}. "
                    f"Fix: {f.suggested_fix}",
                    tags=["learning", "failure", f.phase.lower()],
                    confidence=f.confidence,
                )

            for p in practices:
                store.remember(
                    "finding",
                    f"best_practice:{p.practice_id}",
                    p.description,
                    tags=["learning", "best_practice", p.category],
                    confidence=p.confidence,
                )

            for prof in profiles:
                if prof.sample_size >= 3:
                    store.remember(
                        "fact",
                        f"model_profile:{prof.provider}:{prof.task_type}",
                        (f"{prof.provider}/{prof.model} success rate: {prof.success_rate:.0%}, "
                         f"avg {prof.avg_duration_s:.0f}s, ${prof.avg_cost_usd:.6f}/call"),
                        tags=["learning", "model_profile", prof.provider],
                        confidence=min(1.0, prof.sample_size / 10),
                    )

            log.info("Learning findings persisted to memory store")
        except Exception as e:
            log.warning("Could not persist to memory store", error=str(e))

    def format_full_report(self) -> str:
        """Generate a formatted report for CLI output."""
        try:
            tasks   = self.reader.load_all_tasks()
            usage   = self.reader.load_token_usage()
        except FileNotFoundError as e:
            return f"  Error: {e}\n  Run at least one task first."

        total  = len(tasks)
        done   = sum(1 for t in tasks if t.get("state") == "COMPLETE")
        failed = sum(1 for t in tasks if t.get("state") == "FAILED")

        failures  = FailurePatternDetector().analyse(tasks)
        practices = BestPracticesExtractor().extract(tasks)
        profiles  = ModelCapabilityProfiler().profile(tasks, usage)
        playbooks = TaskPlaybookGenerator().generate(tasks)

        recs = ModelCapabilityProfiler().routing_recommendations(profiles)

        lines = [
            "",
            "  ── FORGE Learning Report ──────────────────────────────────────",
            f"  Tasks: {total} total  {done} completed  {failed} failed",
            "",
            "  FAILURE PATTERNS",
            FailurePatternDetector().format_report(failures),
            "  BEST PRACTICES",
            BestPracticesExtractor().format_report(practices),
            "  MODEL PROFILES",
            ModelCapabilityProfiler().format_report(profiles),
        ]
        if recs:
            lines += ["", "  ROUTING RECOMMENDATIONS"] + [f"  {r}" for r in recs]
        lines += [
            "",
            "  PLAYBOOKS",
            TaskPlaybookGenerator().format_report(playbooks),
            "",
        ]
        return "\n".join(lines)