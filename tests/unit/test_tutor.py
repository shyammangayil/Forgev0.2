"""
Unit tests for forge.tutor — Evidence, Principles, Store, SelfTutor.
Pure stdlib unittest — no network, no LLM, no real DB.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.tutor.evidence import (
    parse_trace, evidence_from_trace, evidence_from_spec,
    TraceEvent, Evidence,
)
from forge.tutor.principles import (
    Principle, PrincipleEngine,
    MIN_CONFIDENCE, HIGH_CONFIDENCE,
    CONFIDENCE_RISE, CONFIDENCE_FALL,
    _make_id,
    TraceExtractor, SpecExtractor, RepoExtractor, TaskOutcomeExtractor,
)
from forge.tutor.store import PrincipleStore
from forge.tutor.tutor import SelfTutor


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_principle(
    category="architecture",
    statement="Use ABC for provider interfaces.",
    confidence=0.7,
) -> Principle:
    return Principle(
        principle_id = _make_id(category, statement),
        category     = category,
        statement    = statement,
        rationale    = "test rationale",
        confidence   = confidence,
        tags         = [category],
    )


def _trace_line(**kwargs) -> str:
    base = {
        "timestamp": "2026-06-08T12:00:00+00:00",
        "correlation_id": "abc",
        "task_id": "TASK-001",
        "agent": "system",
        "event": "test event",
        "severity": "INFO",
        "logger": "forge.test",
    }
    base.update(kwargs)
    return json.dumps(base)


def _make_trace(events: list[dict]) -> str:
    return "\n".join(_trace_line(**e) for e in events)


def _mock_store(tmpdir: str) -> PrincipleStore:
    """Real PrincipleStore backed by in-memory MemoryStore via temp DB."""
    from forge.memory.store import MemoryStore
    db = str(Path(tmpdir) / "test.db")
    store = PrincipleStore(db_path=db)
    # Patch get_memory_store inside store module
    real_ms = MemoryStore(db_path=db)
    with patch("forge.tutor.store.get_memory_store", return_value=real_ms):
        return store, real_ms


# ── parse_trace ────────────────────────────────────────────────────────────────

class TestParseTrace(unittest.TestCase):

    def test_parses_valid_json_lines(self):
        raw = _make_trace([
            {"agent": "planner", "event": "phase start", "severity": "INFO"},
            {"agent": "implementer", "event": "phase done", "severity": "INFO",
             "duration_ms": 5000},
        ])
        events = parse_trace(raw)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].agent, "planner")
        self.assertEqual(events[1].duration_ms, 5000)

    def test_skips_non_json_lines(self):
        raw = "not json\n" + _trace_line(agent="qa") + "\nalso not json"
        events = parse_trace(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].agent, "qa")

    def test_empty_input_returns_empty(self):
        self.assertEqual(parse_trace(""), [])

    def test_parses_error_fields(self):
        raw = _trace_line(severity="ERROR", error="something broke", agent="reviewer")
        events = parse_trace(raw)
        self.assertEqual(events[0].error, "something broke")
        self.assertEqual(events[0].severity, "ERROR")

    def test_extra_fields_captured(self):
        raw = _trace_line(custom_field="hello")
        events = parse_trace(raw)
        self.assertIn("custom_field", events[0].extra)


# ── evidence_from_trace ────────────────────────────────────────────────────────

class TestEvidenceFromTrace(unittest.TestCase):

    def _successful_trace(self) -> str:
        return _make_trace([
            {"agent": "planner",     "event": "phase start"},
            {"agent": "implementer", "event": "Test Intelligence complete"},
            {"agent": "reviewer",    "event": "phase done"},
            {"agent": "qa",          "event": "phase done"},
        ])

    def _failed_trace_with_deterministic(self) -> str:
        return _make_trace([
            {"agent": "planner",  "event": "phase start"},
            {"agent": "reviewer", "event": "Supervisor: deterministic gate rejection",
             "severity": "INFO"},
            {"agent": "reviewer", "event": "Supervisor: task FAILED", "severity": "ERROR",
             "error": "Review REJECTED"},
        ])

    def test_evidence_type_is_trace(self):
        ev = evidence_from_trace(self._successful_trace(), "TASK-001")
        self.assertEqual(ev.source_type, "trace")

    def test_outcome_complete_when_no_errors(self):
        ev = evidence_from_trace(self._successful_trace())
        self.assertEqual(ev.payload["outcome"], "COMPLETE")

    def test_outcome_failed_when_errors(self):
        ev = evidence_from_trace(self._failed_trace_with_deterministic())
        self.assertEqual(ev.payload["outcome"], "FAILED")

    def test_deterministic_rejections_captured(self):
        ev = evidence_from_trace(self._failed_trace_with_deterministic())
        self.assertGreater(len(ev.payload["deterministic_rejections"]), 0)

    def test_agents_seen_populated(self):
        ev = evidence_from_trace(self._successful_trace())
        self.assertIn("planner", ev.payload["agents_seen"])

    def test_quality_nonzero_for_real_trace(self):
        ev = evidence_from_trace(self._successful_trace())
        self.assertGreater(ev.quality, 0.5)

    def test_empty_trace_low_quality(self):
        ev = evidence_from_trace("")
        self.assertLess(ev.quality, 0.5)


# ── evidence_from_spec ─────────────────────────────────────────────────────────

class TestEvidenceFromSpec(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_reads_requirements_md(self):
        path = self._write("requirements.md", "# Requirements\n- Must handle auth\n- Must be fast\n")
        ev = evidence_from_spec(path)
        self.assertEqual(ev.source_type, "spec")
        self.assertIn("Requirements", ev.payload["headings"])

    def test_extracts_rules(self):
        path = self._write("design.md",
            "# Design\n- Must use async\n- Never expose secrets\n"
            "must validate input\nalways sanitize output\n")
        ev = evidence_from_spec(path)
        self.assertGreater(len(ev.payload["rules"]), 0)

    def test_nonexistent_file_zero_quality(self):
        ev = evidence_from_spec("/nonexistent/path/file.md")
        self.assertEqual(ev.quality, 0.0)
        self.assertIn("error", ev.payload)

    def test_headings_extracted(self):
        path = self._write("tasks.md",
            "# Tasks\n## Phase 1\n### Step 1\n")
        ev = evidence_from_spec(path)
        self.assertIn("Tasks", ev.payload["headings"])

    def test_decision_patterns_captured(self):
        path = self._write("ADR-001.md",
            "# ADR-001\n## Context\nWe need a DB.\n## Decision\nUse SQLite.\n## Consequences\nSimple.\n")
        ev = evidence_from_spec(path)
        self.assertGreater(len(ev.payload["decisions"]), 0)


# ── Principle ──────────────────────────────────────────────────────────────────

class TestPrinciple(unittest.TestCase):

    def test_confirm_increases_confidence(self):
        p = _make_principle(confidence=0.6)
        before = p.confidence
        p.confirm()
        self.assertGreater(p.confidence, before)

    def test_contradict_decreases_confidence(self):
        p = _make_principle(confidence=0.6)
        before = p.confidence
        p.contradict()
        self.assertLess(p.confidence, before)

    def test_confirm_saturates_at_max_confirmations(self):
        p = _make_principle(confidence=0.5)
        from forge.tutor.principles import MAX_CONFIRMATION
        for _ in range(MAX_CONFIRMATION + 5):
            p.confirm()
        self.assertLessEqual(p.confirmations, MAX_CONFIRMATION)

    def test_active_false_below_min_confidence(self):
        p = _make_principle(confidence=MIN_CONFIDENCE + 0.01)
        p.contradict()
        p.contradict()
        if p.confidence < MIN_CONFIDENCE:
            self.assertFalse(p.active)

    def test_core_set_when_high_confidence(self):
        p = _make_principle(confidence=HIGH_CONFIDENCE - 0.01)
        self.assertFalse(p.core)
        p.confirm()
        if p.confidence >= HIGH_CONFIDENCE:
            self.assertTrue(p.core)

    def test_roundtrip_serialisation(self):
        p1 = _make_principle()
        p1.confirmations = 3
        p1.core = True
        p2 = Principle.from_dict(p1.to_dict())
        self.assertEqual(p2.principle_id,  p1.principle_id)
        self.assertEqual(p2.confidence,    p1.confidence)
        self.assertEqual(p2.confirmations, p1.confirmations)
        self.assertEqual(p2.core,          p1.core)

    def test_to_prompt_line_format(self):
        p = _make_principle(category="testing", statement="Write tests before code.")
        line = p.to_prompt_line()
        self.assertIn("[testing]", line)
        self.assertIn("Write tests before code.", line)

    def test_make_id_deterministic(self):
        id1 = _make_id("architecture", "Use ABC for providers.")
        id2 = _make_id("architecture", "Use ABC for providers.")
        self.assertEqual(id1, id2)

    def test_make_id_different_for_different_inputs(self):
        id1 = _make_id("architecture", "Use ABC.")
        id2 = _make_id("testing",      "Use ABC.")
        self.assertNotEqual(id1, id2)


# ── Extractors ─────────────────────────────────────────────────────────────────

class TestTraceExtractor(unittest.TestCase):

    def setUp(self):
        self.ext = TraceExtractor()

    def test_deterministic_rejection_produces_verification_principle(self):
        ev = Evidence(
            source_type="trace", source_id="t1",
            payload={
                "outcome": "FAILED",
                "deterministic_rejections": ["deterministic gate rejection"],
                "recovery_events": [],
                "providers_used": [],
                "error_events": [],
                "warning_events": [],
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("verification", categories)

    def test_recovery_events_produces_error_recovery_principle(self):
        ev = Evidence(
            source_type="trace", source_id="t2",
            payload={
                "outcome": "COMPLETE",
                "deterministic_rejections": [],
                "recovery_events": ["repair context injected"],
                "providers_used": [],
                "error_events": [],
                "warning_events": [],
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("error_recovery", categories)

    def test_multiple_providers_produces_architecture_principle(self):
        ev = Evidence(
            source_type="trace", source_id="t3",
            payload={
                "outcome": "COMPLETE",
                "deterministic_rejections": [],
                "recovery_events": [],
                "providers_used": ["anthropic", "openai"],
                "error_events": [],
                "warning_events": [],
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("architecture", categories)

    def test_wrong_source_type_returns_empty(self):
        ev = Evidence(source_type="spec", source_id="x", payload={})
        self.assertEqual(self.ext.extract(ev), [])


class TestSpecExtractor(unittest.TestCase):

    def setUp(self):
        self.ext = SpecExtractor()

    def test_requirements_md_produces_planning_principle(self):
        ev = Evidence(
            source_type="spec", source_id="/req/requirements.md",
            payload={
                "title": "requirements",
                "headings": ["Overview", "Constraints"],
                "rules": ["must be fast", "never expose secrets", "must validate input",
                          "shall handle errors", "always log"],
                "decisions": [],
                "file_refs": [],
                "word_count": 200,
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("planning", categories)

    def test_adr_produces_architecture_principle(self):
        ev = Evidence(
            source_type="spec", source_id="/docs/adr-001.md",
            payload={
                "title": "adr-001",
                "headings": ["Context", "Decision", "Consequences"],
                "rules": [],
                "decisions": ["## Decision\nUse SQLite."],
                "file_refs": [],
                "word_count": 100,
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("architecture", categories)

    def test_wrong_source_type_returns_empty(self):
        ev = Evidence(source_type="trace", source_id="x", payload={})
        self.assertEqual(self.ext.extract(ev), [])


class TestRepoExtractor(unittest.TestCase):

    def setUp(self):
        self.ext = RepoExtractor()

    def test_registry_pattern_produces_architecture_principle(self):
        ev = Evidence(
            source_type="repo_scan", source_id="myrepo",
            payload={
                "repo_name": "myrepo",
                "patterns": [{"pattern": "Registry", "confidence": 0.8, "evidence": "..."}],
                "layer_map": {},
                "frameworks": [],
            },
        )
        results = self.ext.extract(ev)
        stmts = [p.statement for p, _ in results]
        self.assertTrue(any("Registry" in s for s in stmts))

    def test_strategy_abstract_produces_principle(self):
        ev = Evidence(
            source_type="repo_scan", source_id="myrepo",
            payload={
                "repo_name": "myrepo",
                "patterns": [{"pattern": "Strategy/Abstract", "confidence": 0.78, "evidence": "..."}],
                "layer_map": {},
                "frameworks": [],
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("architecture", categories)

    def test_layered_architecture_produces_principle(self):
        ev = Evidence(
            source_type="repo_scan", source_id="myrepo",
            payload={
                "repo_name": "myrepo",
                "patterns": [],
                "layer_map": {
                    "api/routes": [], "agents": [], "llm/providers": [],
                    "memory/storage": [], "config": [],
                },
                "frameworks": [],
            },
        )
        results = self.ext.extract(ev)
        stmts = [p.statement for p, _ in results]
        self.assertTrue(any("layer" in s.lower() for s in stmts))

    def test_wrong_source_type_returns_empty(self):
        ev = Evidence(source_type="task_outcome", source_id="x", payload={})
        self.assertEqual(self.ext.extract(ev), [])


class TestTaskOutcomeExtractor(unittest.TestCase):

    def setUp(self):
        self.ext = TaskOutcomeExtractor()

    def test_successful_with_repairs_confirms_recovery_principle(self):
        ev = Evidence(
            source_type="task_outcome", source_id="TASK-001",
            payload={
                "task_id": "TASK-001",
                "task_type": "coding",
                "outcome": "COMPLETE",
                "success": True,
                "error": "",
                "repair_attempts": 2,
                "tests_passed": True,
                "ti_summary": {"total_functions": 5},
                "repo_as_is": False,
            },
        )
        results = self.ext.extract(ev)
        actions = [action for _, action in results]
        self.assertIn("confirm", actions)

    def test_repo_as_is_success_confirms_planning_principle(self):
        ev = Evidence(
            source_type="task_outcome", source_id="TASK-002",
            payload={
                "task_id": "TASK-002",
                "task_type": "analysis",
                "outcome": "COMPLETE",
                "success": True,
                "error": "",
                "repair_attempts": 0,
                "tests_passed": True,
                "ti_summary": {},
                "repo_as_is": True,
            },
        )
        results = self.ext.extract(ev)
        categories = [p.category for p, _ in results]
        self.assertIn("planning", categories)

    def test_wrong_source_type_returns_empty(self):
        ev = Evidence(source_type="trace", source_id="x", payload={})
        self.assertEqual(self.ext.extract(ev), [])


# ── PrincipleEngine ────────────────────────────────────────────────────────────

class TestPrincipleEngine(unittest.TestCase):

    def setUp(self):
        self.engine = PrincipleEngine()

    def test_process_returns_list(self):
        ev = Evidence(
            source_type="trace", source_id="t",
            payload={
                "outcome": "FAILED",
                "deterministic_rejections": ["deterministic gate rejection — not retrying"],
                "recovery_events": [],
                "providers_used": [],
                "error_events": [],
                "warning_events": [],
            },
        )
        results = self.engine.process(ev)
        self.assertIsInstance(results, list)
        for p, action in results:
            self.assertIsInstance(p, Principle)
            self.assertIn(action, ("add", "confirm", "contradict"))

    def test_batch_cross_source_confirmation(self):
        """Same principle from two sources should produce a 'confirm' action."""
        ev1 = Evidence(
            source_type="repo_scan", source_id="repo-a",
            payload={
                "repo_name": "repo-a",
                "patterns": [{"pattern": "Registry", "confidence": 0.8, "evidence": "..."}],
                "layer_map": {},
                "frameworks": [],
            },
        )
        ev2 = Evidence(
            source_type="repo_scan", source_id="repo-b",
            payload={
                "repo_name": "repo-b",
                "patterns": [{"pattern": "Registry", "confidence": 0.8, "evidence": "..."}],
                "layer_map": {},
                "frameworks": [],
            },
        )
        results = self.engine.process_batch([ev1, ev2])
        actions = [action for _, action in results]
        # The same principle from two different source_ids should yield a confirm
        self.assertIn("confirm", actions)

    def test_extractor_failure_does_not_crash_engine(self):
        """A broken extractor must not prevent other extractors from running."""
        broken = MagicMock()
        broken.extract.side_effect = RuntimeError("broken extractor")
        engine = PrincipleEngine()
        engine._extractors.insert(0, broken)
        ev = Evidence(source_type="spec", source_id="x",
                      payload={"title": "requirements", "headings": [],
                               "rules": ["must be fast"]*6,
                               "decisions": [], "file_refs": [], "word_count": 50})
        # Should not raise
        results = engine.process(ev)
        self.assertIsInstance(results, list)


# ── PrincipleStore ─────────────────────────────────────────────────────────────

class TestPrincipleStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db     = str(Path(self.tmpdir) / "test.db")
        from forge.memory.store import MemoryStore
        self.ms     = MemoryStore(db_path=self.db)
        self.store  = PrincipleStore(db_path=self.db)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patched_store(self):
        return patch("forge.tutor.store.get_memory_store", return_value=self.ms)

    def test_upsert_new_principle(self):
        p = _make_principle()
        with self._patched_store():
            result = self.store.upsert(p, "add")
        self.assertEqual(result.principle_id, p.principle_id)

    def test_upsert_existing_confirms(self):
        p = _make_principle(confidence=0.6)
        with self._patched_store():
            self.store.upsert(p, "add")
            result = self.store.upsert(p, "add")
        self.assertGreater(result.confidence, 0.6)
        self.assertGreater(result.confirmations, 0)

    def test_upsert_contradict_decreases_confidence(self):
        p = _make_principle(confidence=0.7)
        with self._patched_store():
            self.store.upsert(p, "add")
            result = self.store.upsert(p, "contradict")
        self.assertLess(result.confidence, 0.7)

    def test_list_active_returns_principles(self):
        p = _make_principle()
        with self._patched_store():
            self.store.upsert(p, "add")
            active = self.store.list_active()
        self.assertTrue(any(x.principle_id == p.principle_id for x in active))

    def test_list_active_filters_by_category(self):
        p1 = _make_principle(category="testing",      statement="Write tests early.")
        p2 = _make_principle(category="architecture", statement="Use layers.")
        with self._patched_store():
            self.store.upsert(p1, "add")
            self.store.upsert(p2, "add")
            testing_only = self.store.list_active(category="testing")
        self.assertTrue(all(x.category == "testing" for x in testing_only))

    def test_list_core_returns_only_high_confidence(self):
        low  = _make_principle(confidence=0.5, statement="Low confidence.")
        high = _make_principle(confidence=0.9, statement="High confidence.")
        high.core = True
        with self._patched_store():
            self.store.upsert(low,  "add")
            self.store.upsert(high, "add")
            core = self.store.list_core()
        core_ids = [p.principle_id for p in core]
        self.assertIn(high.principle_id, core_ids)
        self.assertNotIn(low.principle_id, core_ids)

    def test_to_prompt_block_non_empty_when_principles_exist(self):
        p = _make_principle(confidence=0.6)
        with self._patched_store():
            self.store.upsert(p, "add")
            block = self.store.to_prompt_block()
        self.assertIn("ENGINEERING PRINCIPLES", block)
        self.assertIn(p.statement, block)

    def test_stats_returns_counts(self):
        p = _make_principle()
        with self._patched_store():
            self.store.upsert(p, "add")
            stats = self.store.stats()
        self.assertIn("total",    stats)
        self.assertIn("active",   stats)
        self.assertIn("core",     stats)
        self.assertIn("inactive", stats)


# ── SelfTutor integration ──────────────────────────────────────────────────────

class TestSelfTutor(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db     = str(Path(self.tmpdir) / "test.db")
        from forge.memory.store import MemoryStore
        self.ms     = MemoryStore(db_path=self.db)
        self.tutor  = SelfTutor(db_path=self.db)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _patch(self):
        return patch("forge.tutor.store.get_memory_store", return_value=self.ms)

    def test_learn_from_trace_returns_list(self):
        trace = _make_trace([
            {"agent": "reviewer", "event": "deterministic gate rejection", "severity": "INFO"},
            {"agent": "reviewer", "event": "task FAILED", "severity": "ERROR"},
        ])
        with self._patch():
            result = self.tutor.learn_from_trace(trace, "TASK-TEST")
        self.assertIsInstance(result, list)

    def test_learn_from_spec_dir(self):
        spec_dir = Path(self.tmpdir) / "specs"
        spec_dir.mkdir()
        (spec_dir / "requirements.md").write_text(
            "# Requirements\n- must be fast\n- must handle auth\n"
            "- never expose secrets\n- must validate input\n"
            "- shall log all operations\n- always sanitize\n"
        )
        with self._patch():
            result = self.tutor.learn_from_spec_dir(str(spec_dir))
        self.assertIsInstance(result, list)

    def test_learn_from_spec_dir_empty_returns_empty(self):
        empty = Path(self.tmpdir) / "empty"
        empty.mkdir()
        with self._patch():
            result = self.tutor.learn_from_spec_dir(str(empty))
        self.assertEqual(result, [])

    def test_enrich_prompt_appends_principles_when_available(self):
        # Manually upsert a principle first
        p = _make_principle(category="planning", confidence=0.7,
                            statement="Write specs before code.")
        with self._patch():
            self.tutor._store.upsert(p, "add")
            enriched = self.tutor.enrich_prompt(
                "You are a planner.", agent_name="planner"
            )
        # Should contain the original prompt
        self.assertIn("You are a planner.", enriched)
        # Should contain the principle block
        self.assertIn("ENGINEERING PRINCIPLES", enriched)

    def test_enrich_prompt_returns_original_when_no_principles(self):
        original = "You are a planner."
        with self._patch():
            result = self.tutor.enrich_prompt(original, agent_name="planner")
        self.assertIn(original, result)

    def test_report_returns_string(self):
        with self._patch():
            report = self.tutor.report()
        self.assertIsInstance(report, str)

    def test_stats_returns_dict(self):
        with self._patch():
            stats = self.tutor.stats()
        self.assertIn("total",  stats)
        self.assertIn("active", stats)

    def test_learn_from_task_non_fatal_on_bad_task(self):
        """learn_from_task must not raise even with a malformed task object."""
        bad_task = MagicMock()
        bad_task.id = "X"
        bad_task.task_type = MagicMock()
        bad_task.task_type.value = "coding"
        bad_task.context = {}
        bad_result = MagicMock()
        bad_result.success = True
        bad_result.error = None
        bad_result.tokens_used = 0
        bad_result.provider_used = ""
        bad_result.validation_passed = True
        bad_result.review_passed = True
        bad_result.tests_passed = True
        with self._patch():
            result = self.tutor.learn_from_task(bad_task, bad_result)
        self.assertIsInstance(result, list)

    def test_learn_from_repo_non_fatal_on_bad_path(self):
        """learn_from_repo must not raise on nonexistent path."""
        with self._patch():
            result = self.tutor.learn_from_repo("/nonexistent/path")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
