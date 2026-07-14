"""
FORGE Tutor — Principle Engine
================================
Extracts reusable engineering principles from Evidence objects.

A Principle is a generalised, task-independent engineering behaviour
extracted from patterns that repeat across evidence sources.

Categories (from the spec):
  decomposition   — how to break problems into verifiable subtasks
  architecture    — structural habits that appear in successful repos
  verification    — methods for confirming correctness at each phase
  error_recovery  — patterns for handling and recovering from failures
  planning        — approaches for scoping, sequencing, and prioritising
  testing         — what to test, how to test, when to test
  security        — patterns that prevent vulnerabilities
  observability   — logging, tracing, metrics patterns

Confidence model:
  Initial confidence comes from source quality.
  Confidence RISES when the same principle is observed in a new
    independent source (Bayesian-style update toward 1.0).
  Confidence FALLS when contradicting evidence appears.
  Principles below MIN_CONFIDENCE are soft-deleted (kept but not served).
  Principles above HIGH_CONFIDENCE are promoted as "core behaviours"
    and injected into every agent's system prompt via _inject_memories.

Design constraints:
  - Never give task-specific advice. "Use sqlite for this task" is not
    a principle. "Prefer embedded databases for single-process services"
    is.
  - Never worship any tool. A principle about Claude Code is exactly as
    credible as one about humans or Forge. Source diversity increases
    confidence; monoculture decreases it.
  - Prefer behaviours that appear in multiple successful repositories
    over behaviours from a single high-quality source.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from forge.tutor.evidence import Evidence

# ── Config ─────────────────────────────────────────────────────────────────────

MIN_CONFIDENCE    = 0.20   # below this: principle is inactive
HIGH_CONFIDENCE   = 0.80   # above this: promoted to core behaviour
CONFIDENCE_RISE   = 0.12   # per confirming observation
CONFIDENCE_FALL   = 0.18   # per contradicting observation
MAX_CONFIRMATION  = 8      # Bayesian saturation — diminishing returns


# ── Principle dataclass ────────────────────────────────────────────────────────

@dataclass
class Principle:
    """
    A reusable, generalised engineering principle.

    principle_id:    stable hash of (category, statement)
    category:        see CATEGORIES above
    statement:       the principle, written as a general rule
    rationale:       why — evidence chain summary
    confidence:      0.0–1.0
    source_count:    distinct sources that contributed
    confirmations:   times this was re-observed (increases confidence)
    contradictions:  times this was contradicted (decreases confidence)
    tags:            for filtering and injection targeting
    active:          False when confidence < MIN_CONFIDENCE
    core:            True when confidence > HIGH_CONFIDENCE (promoted)
    """
    principle_id:   str
    category:       str
    statement:      str
    rationale:      str
    confidence:     float
    source_count:   int   = 1
    confirmations:  int   = 0
    contradictions: int   = 0
    tags:           List[str] = field(default_factory=list)
    active:         bool  = True
    core:           bool  = False

    def confirm(self, delta: float = CONFIDENCE_RISE) -> None:
        """Increase confidence — new independent source agrees."""
        if self.confirmations >= MAX_CONFIRMATION:
            return
        self.confirmations += 1
        self.source_count  += 1
        # Bayesian-style: move toward 1.0 with diminishing returns
        gap = 1.0 - self.confidence
        self.confidence = round(min(1.0, self.confidence + gap * delta), 4)
        self.core = self.confidence >= HIGH_CONFIDENCE

    def contradict(self, delta: float = CONFIDENCE_FALL) -> None:
        """Decrease confidence — contradicting evidence found."""
        self.contradictions += 1
        self.confidence = round(max(0.0, self.confidence - delta), 4)
        self.active = self.confidence >= MIN_CONFIDENCE
        self.core   = self.confidence >= HIGH_CONFIDENCE

    def to_prompt_line(self) -> str:
        """One-line form for agent prompt injection."""
        return f"[{self.category}] {self.statement}"

    def to_dict(self) -> dict:
        return {
            "id":            self.principle_id,
            "category":      self.category,
            "statement":     self.statement,
            "rationale":     self.rationale,
            "confidence":    self.confidence,
            "sources":       self.source_count,
            "confirmations": self.confirmations,
            "contradictions":self.contradictions,
            "tags":          self.tags,
            "active":        self.active,
            "core":          self.core,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Principle":
        return cls(
            principle_id  = d["id"],
            category      = d["category"],
            statement     = d["statement"],
            rationale     = d.get("rationale", ""),
            confidence    = float(d.get("confidence", 0.5)),
            source_count  = int(d.get("sources", 1)),
            confirmations = int(d.get("confirmations", 0)),
            contradictions= int(d.get("contradictions", 0)),
            tags          = d.get("tags", []),
            active        = bool(d.get("active", True)),
            core          = bool(d.get("core", False)),
        )


def _make_id(category: str, statement: str) -> str:
    raw = f"{category}:{statement[:80]}"
    return hashlib.md5(raw.encode()).hexdigest()[:10]


# ── Extractors ─────────────────────────────────────────────────────────────────

class TraceExtractor:
    """
    Extracts principles from execution traces.

    Looks for:
      - deterministic rejections → verification principles
      - repair/replan cycles     → error_recovery principles
      - phase duration patterns  → planning principles
      - provider failures        → architecture principles
      - test intelligence events → testing principles
    """

    def extract(self, evidence: Evidence) -> List[Tuple[Principle, str]]:
        """
        Returns list of (Principle, update_action) where
        update_action is 'add', 'confirm', or 'contradict'.
        """
        if evidence.source_type != "trace":
            return []

        p   = evidence.payload
        out = []

        # ── Deterministic gate rejection pattern ──────────────────────────
        if p.get("deterministic_rejections"):
            out.append((Principle(
                principle_id = _make_id("verification",
                                        "deterministic-rejection-no-retry"),
                category    = "verification",
                statement   = "When a verification gate produces a clear REJECTED/CRITICAL "
                              "verdict, do not retry — the same code will produce the same "
                              "verdict. Address the root cause instead.",
                rationale   = "Observed: deterministic rejection logged, task terminated "
                              "without retry. Retrying known-bad code wastes budget.",
                confidence  = 0.85 * evidence.quality,
                tags        = ["verification", "retry", "gate"],
            ), "add"))

        # ── Repair/replan cycle pattern ───────────────────────────────────
        if p.get("recovery_events"):
            out.append((Principle(
                principle_id = _make_id("error_recovery",
                                        "repair-context-injection"),
                category    = "error_recovery",
                statement   = "On transient failure, inject structured repair context "
                              "(failed phase, reason, consensus score, budget remaining) "
                              "before replanning. Generic retry without context repeats "
                              "the same mistake.",
                rationale   = "Observed: repair context injected before replan in "
                              f"{len(p['recovery_events'])} recovery event(s).",
                confidence  = 0.75 * evidence.quality,
                tags        = ["error_recovery", "replan", "context"],
            ), "add"))

        # ── Multiple providers pattern ─────────────────────────────────────
        if len(p.get("providers_used", [])) > 1:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "provider-fallback-chain"),
                category    = "architecture",
                statement   = "Route LLM calls through an ordered fallback chain rather "
                              "than a single provider. Transient failures should trigger "
                              "automatic failover, not task failure.",
                rationale   = f"Observed multiple providers in one task: "
                              f"{p.get('providers_used')}.",
                confidence  = 0.70 * evidence.quality,
                tags        = ["architecture", "resilience", "provider"],
            ), "add"))

        # ── Test intelligence present → testing principle ─────────────────
        if p.get("outcome") == "COMPLETE" and any(
            "Test Intelligence" in e for e in
            [ev.get("event", "") for ev in p.get("error_events", [])] +
            [str(p)]
        ):
            out.append((Principle(
                principle_id = _make_id("testing",
                                        "static-analysis-before-test-generation"),
                category    = "testing",
                statement   = "Run static analysis on generated code before writing tests. "
                              "Critical paths, risk areas, and dead code should be known "
                              "before the QA agent writes a single test case.",
                rationale   = "Observed: Test Intelligence analysis completed before "
                              "QA phase in successful task.",
                confidence  = 0.72 * evidence.quality,
                tags        = ["testing", "static_analysis", "qa"],
            ), "add"))

        return out


class SpecExtractor:
    """
    Extracts principles from specification documents.

    Looks for:
      - explicit rules (must/shall/never)
      - section structure  → decomposition principles
      - ADR decision patterns → architecture principles
      - task breakdown structure → planning principles
    """

    # Spec files that indicate specification-first development
    _SPEC_FIRST_FILES = {
        "requirements", "requirements.md",
        "design", "design.md",
        "tasks", "tasks.md",
        "adr", "spec", "spec.md",
        "claude", "claude.md",
        "architecture", "architecture.md",
    }

    def extract(self, evidence: Evidence) -> List[Tuple[Principle, str]]:
        if evidence.source_type != "spec":
            return []

        p     = evidence.payload
        title = p.get("title", "").lower()
        out   = []

        # ── Specification-first pattern ───────────────────────────────────
        if any(sig in title for sig in self._SPEC_FIRST_FILES):
            out.append((Principle(
                principle_id = _make_id("planning",
                                        "specification-first"),
                category    = "planning",
                statement   = "Write requirements → design → tasks before any implementation. "
                              "A specification document acts as ground truth that all agents "
                              "can reference; implementation without spec produces unmaintainable "
                              "code.",
                rationale   = f"Observed specification document: {p.get('title')}. "
                              f"Contains {len(p.get('rules', []))} explicit rules and "
                              f"{len(p.get('headings', []))} sections.",
                confidence  = 0.78 * evidence.quality,
                tags        = ["planning", "spec_first", "decomposition"],
            ), "add"))

        # ── Explicit rules → verification principle ───────────────────────
        rules = p.get("rules", [])
        if len(rules) >= 5:
            out.append((Principle(
                principle_id = _make_id("verification",
                                        "explicit-acceptance-criteria"),
                category    = "verification",
                statement   = "Every task must carry explicit, machine-checkable acceptance "
                              "criteria. 'Works correctly' is not a criterion. "
                              "'Returns 401 when token is absent' is.",
                rationale   = f"Observed {len(rules)} explicit rules in {p.get('title')}. "
                              "Specs with dense rule lists produce more verifiable tasks.",
                confidence  = 0.74 * evidence.quality,
                tags        = ["verification", "criteria", "spec"],
            ), "add"))

        # ── ADR decision pattern ──────────────────────────────────────────
        if p.get("decisions") and "adr" in title:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "adr-decision-records"),
                category    = "architecture",
                statement   = "Record architectural decisions as explicit documents "
                              "(ADRs) with context, decision, and consequences. "
                              "Undocumented decisions become mystery constraints.",
                rationale   = f"Observed {len(p['decisions'])} decision(s) in ADR document.",
                confidence  = 0.76 * evidence.quality,
                tags        = ["architecture", "adr", "documentation"],
            ), "add"))

        # ── Incremental tasks pattern ─────────────────────────────────────
        if "task" in title and p.get("headings"):
            headings_lower = " ".join(p["headings"]).lower()
            if any(kw in headings_lower for kw in
                   ("phase", "milestone", "increment", "iteration", "step")):
                out.append((Principle(
                    principle_id = _make_id("decomposition",
                                            "incremental-phases"),
                    category    = "decomposition",
                    statement   = "Break implementation into sequential phases (not just tasks). "
                                  "Each phase should produce a testable, deployable artefact. "
                                  "Phases make partial completion visible and resumable.",
                    rationale   = f"Observed phase/milestone structure in {p.get('title')}.",
                    confidence  = 0.70 * evidence.quality,
                    tags        = ["decomposition", "planning", "incremental"],
                ), "add"))

        return out


class RepoExtractor:
    """
    Extracts architecture and pattern principles from repository scans.
    Studies both internal Forge repos and external reference repos.
    """

    def extract(self, evidence: Evidence) -> List[Tuple[Principle, str]]:
        if evidence.source_type not in ("repo_scan", "external_repo"):
            return []

        p    = evidence.payload
        out  = []
        pats = {pp["pattern"] for pp in p.get("patterns", [])}

        # ── Registry pattern ──────────────────────────────────────────────
        if "Registry" in pats:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "registry-for-extensibility"),
                category    = "architecture",
                statement   = "Use a Registry pattern for extensible collections "
                              "(tools, agents, providers, plugins). A register/get/list "
                              "interface decouples consumers from implementations and "
                              "enables runtime extension without modifying core code.",
                rationale   = f"Observed Registry pattern in {p['repo_name']}.",
                confidence  = 0.78 * evidence.quality,
                tags        = ["architecture", "registry", "extensibility"],
            ), "add"))

        # ── Strategy/Abstract pattern ─────────────────────────────────────
        if "Strategy/Abstract" in pats:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "abstract-base-for-providers"),
                category    = "architecture",
                statement   = "Define provider interfaces as abstract base classes. "
                              "New providers implement the ABC; callers depend on the "
                              "interface, not the implementation. This is the enabling "
                              "condition for fallback chains and mock injection.",
                rationale   = f"Observed Strategy/Abstract in {p['repo_name']}.",
                confidence  = 0.80 * evidence.quality,
                tags        = ["architecture", "abstraction", "provider"],
            ), "add"))

        # ── Layer map → separation of concerns ───────────────────────────
        layer_map = p.get("layer_map", {})
        if len(layer_map) >= 4:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "layered-architecture"),
                category    = "architecture",
                statement   = "Separate code into distinct layers: api/routes, agents, "
                              "llm/providers, tools, memory/storage, config, telemetry. "
                              "Cross-layer imports should flow downward only. "
                              "Circular imports between layers signal design debt.",
                rationale   = f"Observed {len(layer_map)} distinct architecture layers "
                              f"in {p['repo_name']}: {list(layer_map.keys())[:5]}.",
                confidence  = 0.76 * evidence.quality,
                tags        = ["architecture", "layers", "separation"],
            ), "add"))

        # ── Observer pattern → event-driven recovery ─────────────────────
        if "Observer/Event" in pats:
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "event-driven-decoupling"),
                category    = "architecture",
                statement   = "Decouple agents from each other using events/callbacks "
                              "rather than direct calls where ordering is not required. "
                              "Event buses enable observability and make side effects "
                              "explicit.",
                rationale   = f"Observed Observer/Event pattern in {p['repo_name']}.",
                confidence  = 0.68 * evidence.quality,
                tags        = ["architecture", "events", "decoupling"],
            ), "add"))

        # ── Multi-LLM delegation ──────────────────────────────────────────
        fws = p.get("frameworks", [])
        if any("SDK" in fw or "LLM" in fw.upper() or fw.lower() in
               ("anthropic sdk", "openai sdk", "langchain") for fw in fws):
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "multi-llm-abstraction"),
                category    = "architecture",
                statement   = "Abstract LLM provider calls behind a router that supports "
                              "multiple backends. Task-type routing (coding → model A, "
                              "review → model B) and fallback chains are properties of "
                              "the router, not individual agents.",
                rationale   = f"Observed multi-LLM support in {p['repo_name']} "
                              f"(frameworks: {fws[:3]}).",
                confidence  = 0.75 * evidence.quality,
                tags        = ["architecture", "llm", "routing"],
            ), "add"))

        # ── Memory/storage layer ──────────────────────────────────────────
        if any("memory" in l.lower() or "storage" in l.lower()
               for l in layer_map.keys()):
            out.append((Principle(
                principle_id = _make_id("architecture",
                                        "persistent-cross-task-memory"),
                category    = "architecture",
                statement   = "Persist agent findings (failure patterns, best practices, "
                              "risk areas) to a cross-task memory store. Memory must be "
                              "queryable by future agents — episodic memory (what happened "
                              "on task X) AND semantic memory (what is generally true).",
                rationale   = f"Observed memory/storage layer in {p['repo_name']}.",
                confidence  = 0.74 * evidence.quality,
                tags        = ["architecture", "memory", "persistence"],
            ), "add"))

        return out


class TaskOutcomeExtractor:
    """
    Extracts planning and verification principles from task outcomes.
    Tracks what distinguishes successful tasks from failed ones.
    """

    def extract(self, evidence: Evidence) -> List[Tuple[Principle, str]]:
        if evidence.source_type != "task_outcome":
            return []

        p   = evidence.payload
        out = []

        # ── Repair attempts as signal ──────────────────────────────────────
        repairs = p.get("repair_attempts", 0)
        if repairs > 0 and p.get("success"):
            out.append((Principle(
                principle_id = _make_id("error_recovery",
                                        "recovery-with-context-succeeds"),
                category    = "error_recovery",
                statement   = "Structured repair context (failed phase, reason, remaining "
                              "budget) injected before replan consistently enables recovery. "
                              "Unstructured retry without context does not.",
                rationale   = f"Task {p['task_id']} succeeded after {repairs} repair "
                              f"attempt(s). Recovery with context worked.",
                confidence  = 0.72,
                tags        = ["error_recovery", "repair", "success"],
            ), "confirm"))

        elif repairs > 2 and not p.get("success"):
            out.append((Principle(
                principle_id = _make_id("error_recovery",
                                        "recovery-with-context-succeeds"),
                category    = "error_recovery",
                statement   = "Structured repair context (failed phase, reason, remaining "
                              "budget) injected before replan consistently enables recovery. "
                              "Unstructured retry without context does not.",
                rationale   = f"Task {p['task_id']} failed after {repairs} repair attempts.",
                confidence  = 0.72,
                tags        = ["error_recovery"],
            ), "contradict"))

        # ── Test intelligence → verification ──────────────────────────────
        ti = p.get("ti_summary", {})
        if ti and p.get("tests_passed"):
            out.append((Principle(
                principle_id = _make_id("testing",
                                        "static-analysis-before-test-generation"),
                category    = "testing",
                statement   = "Run static analysis on generated code before writing tests. "
                              "Critical paths, risk areas, and dead code should be known "
                              "before the QA agent writes a single test case.",
                rationale   = f"Task {p['task_id']} passed tests with intelligence brief.",
                confidence  = 0.72,
                tags        = ["testing", "qa"],
            ), "confirm"))

        # ── Repository context → planning ─────────────────────────────────
        if p.get("repo_as_is") and p.get("success"):
            out.append((Principle(
                principle_id = _make_id("planning",
                                        "as-is-context-before-planning"),
                category    = "planning",
                statement   = "When modifying an existing codebase, reverse-engineer it "
                              "into an AS-IS prompt before planning. Plans written without "
                              "architectural context produce code that conflicts with "
                              "existing conventions.",
                rationale   = f"Task {p['task_id']} succeeded with AS-IS context attached.",
                confidence  = 0.73,
                tags        = ["planning", "context", "repo"],
            ), "confirm"))

        return out


# ── Principle Engine ───────────────────────────────────────────────────────────

class PrincipleEngine:
    """
    Runs all extractors against Evidence and returns a list of
    (Principle, action) pairs for the PrincipleStore to apply.

    Also detects cross-extractor confirmation: if two independent
    sources produce the same principle_id, that's a confirmation
    regardless of which extractor found it.
    """

    def __init__(self):
        self._extractors = [
            TraceExtractor(),
            SpecExtractor(),
            RepoExtractor(),
            TaskOutcomeExtractor(),
        ]

    def process(self, evidence: Evidence) -> List[Tuple[Principle, str]]:
        """Process one Evidence object, return (Principle, action) pairs."""
        results = []
        for extractor in self._extractors:
            try:
                results.extend(extractor.extract(evidence))
            except Exception:
                pass   # extractor failure must never block the tutor
        return results

    def process_batch(
        self, evidence_list: List[Evidence]
    ) -> List[Tuple[Principle, str]]:
        """
        Process multiple Evidence objects.
        Cross-source confirmation: if the same principle_id appears
        from two different source_ids, upgrade the second to 'confirm'.
        """
        seen_ids: Dict[str, str] = {}   # principle_id → source_id
        all_results = []

        for ev in evidence_list:
            for principle, action in self.process(ev):
                pid = principle.principle_id
                if pid in seen_ids and seen_ids[pid] != ev.source_id:
                    # Different source produced same principle — confirmation
                    action = "confirm"
                else:
                    seen_ids[pid] = ev.source_id
                all_results.append((principle, action))

        return all_results
