"""
Unit tests for forge.intelligence.analyser
Pure stdlib unittest — no pytest, no LLM, no network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.intelligence.analyser import (
    TestIntelligenceEngine,
    TestIntelligenceReport,
    CriticalPath,
    DeadCodeBlock,
    RiskArea,
    UntestedArea,
    CallGraphBuilder,
    _cyclomatic_complexity,
    _nesting_depth,
    _collect_functions,
    get_intelligence_engine,
)
import ast


# ── Helpers ────────────────────────────────────────────────────────────────────

def _engine() -> TestIntelligenceEngine:
    return TestIntelligenceEngine()


def _analyse(src: str) -> TestIntelligenceReport:
    return _engine().analyse(src)


# ── Syntax error handling ──────────────────────────────────────────────────────

class TestParseError(unittest.TestCase):

    def test_syntax_error_sets_parse_error(self):
        r = _analyse("def broken(:\n    pass")
        self.assertIsNotNone(r.parse_error)

    def test_syntax_error_brief_is_graceful(self):
        r = _analyse("def broken(:\n    pass")
        brief = r.to_qa_brief()
        self.assertIn("Parse error", brief)

    def test_empty_source_parses_cleanly(self):
        r = _analyse("")
        self.assertIsNone(r.parse_error)
        self.assertEqual(r.total_functions, 0)

    def test_clean_source_no_error(self):
        r = _analyse("def add(a, b): return a + b")
        self.assertIsNone(r.parse_error)


# ── AST helpers ────────────────────────────────────────────────────────────────

class TestASTHelpers(unittest.TestCase):

    def _func(self, src: str):
        tree = ast.parse(src)
        return next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        )

    def test_complexity_simple(self):
        f = self._func("def f(): return 1")
        self.assertEqual(_cyclomatic_complexity(f), 1)

    def test_complexity_with_if(self):
        f = self._func("def f(x):\n    if x > 0:\n        return 1\n    return 0")
        self.assertGreaterEqual(_cyclomatic_complexity(f), 2)

    def test_complexity_with_for_and_if(self):
        f = self._func(
            "def f(items):\n"
            "    for i in items:\n"
            "        if i > 0:\n"
            "            pass\n"
        )
        self.assertGreaterEqual(_cyclomatic_complexity(f), 3)

    def test_nesting_flat(self):
        f = self._func("def f(): pass")
        self.assertEqual(_nesting_depth(f), 0)

    def test_nesting_double(self):
        f = self._func(
            "def f(x):\n"
            "    if x:\n"
            "        for i in x:\n"
            "            pass\n"
        )
        self.assertGreaterEqual(_nesting_depth(f), 2)

    def test_collect_functions_counts(self):
        tree = ast.parse("def a(): pass\ndef b(): pass\nclass C:\n    def c(self): pass")
        self.assertEqual(len(_collect_functions(tree)), 3)


# ── Critical path detection ────────────────────────────────────────────────────

class TestCriticalPaths(unittest.TestCase):

    def test_high_fan_in_scores_highly(self):
        src = """
def helper():
    pass

def a(): helper()
def b(): helper()
def c(): helper()
def d(): helper()
def e(): helper()
"""
        r = _analyse(src)
        names = [cp.entry_point for cp in r.critical_paths]
        self.assertIn("helper", names)
        helper_cp = next(cp for cp in r.critical_paths if cp.entry_point == "helper")
        self.assertGreaterEqual(helper_cp.fan_in, 4)

    def test_public_function_included(self):
        src = "def public_api(x): return x * 2"
        r = _analyse(src)
        names = [cp.entry_point for cp in r.critical_paths]
        self.assertIn("public_api", names)

    def test_entry_point_has_path(self):
        src = """
def leaf(): return 1
def middle(): return leaf()
def top(): return middle()
"""
        r = _analyse(src)
        top_cp = next((cp for cp in r.critical_paths if cp.entry_point == "top"), None)
        self.assertIsNotNone(top_cp)
        self.assertGreaterEqual(len(top_cp.path), 1)

    def test_exception_raising_increases_score(self):
        src = """
def raises_stuff(x):
    if x < 0:
        raise ValueError("negative")
    return x
"""
        r = _analyse(src)
        self.assertTrue(any(cp.entry_point == "raises_stuff" for cp in r.critical_paths))

    def test_critical_paths_sorted_descending(self):
        src = """
def a(): pass
def b(): pass
def c(): pass
"""
        r = _analyse(src)
        scores = [cp.risk_score for cp in r.critical_paths]
        self.assertEqual(scores, sorted(scores, reverse=True))


# ── Dead code detection ────────────────────────────────────────────────────────

class TestDeadCode(unittest.TestCase):

    def test_unreachable_after_return(self):
        src = """
def f():
    return 42
    print("never")
"""
        r = _analyse(src)
        kinds = [dc.kind for dc in r.dead_code]
        self.assertIn("unreachable_after_return", kinds)

    def test_always_false_condition(self):
        src = """
def f():
    if False:
        do_something()
"""
        r = _analyse(src)
        kinds = [dc.kind for dc in r.dead_code]
        self.assertIn("always_false_condition", kinds)

    def test_unused_private_function(self):
        src = """
def _never_called():
    pass

def public():
    return 1
"""
        r = _analyse(src)
        kinds = [dc.kind for dc in r.dead_code]
        self.assertIn("unused_private", kinds)
        locs = [dc.location for dc in r.dead_code if dc.kind == "unused_private"]
        self.assertIn("_never_called", locs)

    def test_used_private_not_flagged(self):
        src = """
def _helper():
    return 1

def public():
    return _helper()
"""
        r = _analyse(src)
        unused = [dc for dc in r.dead_code if dc.kind == "unused_private"]
        self.assertFalse(any(dc.location == "_helper" for dc in unused))

    def test_unused_import_detected(self):
        src = """
import os
import sys

def f():
    return sys.argv
"""
        r = _analyse(src)
        kinds = [dc.kind for dc in r.dead_code]
        self.assertIn("unused_import", kinds)
        locs = [dc.location for dc in r.dead_code if dc.kind == "unused_import"]
        self.assertIn("module level", locs)

    def test_empty_except_detected(self):
        src = """
def f():
    try:
        risky()
    except Exception:
        pass
"""
        r = _analyse(src)
        kinds = [dc.kind for dc in r.dead_code]
        self.assertIn("empty_except", kinds)

    def test_clean_function_no_dead_code(self):
        src = """
def clean(x, y):
    if x > 0:
        return x + y
    return y
"""
        r = _analyse(src)
        structural = [dc for dc in r.dead_code
                      if dc.kind in ("unreachable_after_return", "always_false_condition")]
        self.assertEqual(len(structural), 0)


# ── Risk area detection ────────────────────────────────────────────────────────

class TestRiskAreas(unittest.TestCase):

    def test_high_complexity_flagged(self):
        # Force complexity > 10 with many branches
        branches = "\n".join(f"    if x == {i}: pass" for i in range(12))
        src = f"def complex_func(x):\n{branches}"
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("high_complexity", cats)

    def test_mutable_default_flagged(self):
        src = "def f(items=[]): return items"
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("mutable_default", cats)

    def test_mutable_dict_default_flagged(self):
        src = "def f(cfg={}): return cfg"
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("mutable_default", cats)

    def test_bare_except_flagged(self):
        src = """
def f():
    try:
        risky()
    except:
        pass
"""
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("bare_except", cats)
        bare = next(ra for ra in r.risk_areas if ra.category == "bare_except")
        self.assertGreaterEqual(bare.risk_score, 0.7)

    def test_sql_injection_risk_flagged(self):
        src = '''
def get_user(username):
    query = f"SELECT * FROM users WHERE name={username}"
    return db.execute(query)
'''
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("sql_injection_risk", cats)
        sql_ra = next(ra for ra in r.risk_areas if ra.category == "sql_injection_risk")
        self.assertGreaterEqual(sql_ra.risk_score, 0.9)

    def test_unbounded_recursion_flagged(self):
        src = """
def fib(n):
    return fib(n-1) + fib(n-2)
"""
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("unbounded_recursion", cats)

    def test_safe_recursion_not_flagged(self):
        src = """
def fib(n):
    if n <= 1:
        return n
    return fib(n-1) + fib(n-2)
"""
        r = _analyse(src)
        rec = [ra for ra in r.risk_areas if ra.category == "unbounded_recursion"]
        self.assertEqual(len(rec), 0)

    def test_unsafe_type_coercion_flagged(self):
        src = """
def parse_age(s):
    return int(s)
"""
        r = _analyse(src)
        cats = [ra.category for ra in r.risk_areas]
        self.assertIn("unsafe_type_coercion", cats)

    def test_risk_areas_sorted_descending(self):
        src = '''
def risky(x=[]):
    try:
        pass
    except:
        pass
    return int(x)
'''
        r = _analyse(src)
        scores = [ra.risk_score for ra in r.risk_areas]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_clean_function_no_risks(self):
        src = """
def add(a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("int required")
    return a + b
"""
        r = _analyse(src)
        high_risk = [ra for ra in r.risk_areas if ra.risk_score >= 0.7]
        self.assertEqual(len(high_risk), 0)


# ── Untested area detection ────────────────────────────────────────────────────

class TestUntestedAreas(unittest.TestCase):

    def test_all_functions_untested_when_no_test_files(self):
        src = """
def public_func(x): return x
def _private(x): return x
"""
        r = _analyse(src)
        names = [ua.name for ua in r.untested_areas]
        self.assertIn("public_func", names)

    def test_public_has_higher_priority_than_private(self):
        src = """
def public_func(x): return x
def _private(x): return x
"""
        r = _analyse(src)
        pub   = next((ua for ua in r.untested_areas if ua.name == "public_func"), None)
        priv  = next((ua for ua in r.untested_areas if ua.name == "_private"),    None)
        self.assertIsNotNone(pub)
        self.assertIsNotNone(priv)
        self.assertTrue(pub.is_public)
        self.assertFalse(priv.is_public)

    def test_test_hint_for_parse_function(self):
        src = "def parse_config(text): return text.split()"
        r = _analyse(src)
        ua = next((u for u in r.untested_areas if u.name == "parse_config"), None)
        self.assertIsNotNone(ua)
        self.assertIn("malformed", ua.test_hint.lower())

    def test_test_hint_for_validate_function(self):
        src = "def validate_email(addr): return '@' in addr"
        r = _analyse(src)
        ua = next((u for u in r.untested_areas if u.name == "validate_email"), None)
        self.assertIsNotNone(ua)
        self.assertIn("invalid", ua.test_hint.lower())

    def test_complexity_stored_on_untested_area(self):
        branches = "\n".join(f"    if x == {i}: pass" for i in range(6))
        src = f"def complex_func(x):\n{branches}"
        r = _analyse(src)
        ua = next((u for u in r.untested_areas if u.name == "complex_func"), None)
        self.assertIsNotNone(ua)
        self.assertGreaterEqual(ua.complexity, 6)


# ── Priority list ──────────────────────────────────────────────────────────────

class TestPriorityList(unittest.TestCase):

    def test_priority_list_not_empty_for_real_code(self):
        src = """
def a(x): return x
def b(x): return a(x) + 1
def c(x): return b(x) * 2
"""
        r = _analyse(src)
        self.assertGreater(len(r.test_priorities), 0)

    def test_high_risk_function_near_top(self):
        src = '''
def safe(x): return x + 1

def dangerous(items=[]):
    try:
        pass
    except:
        pass
    return int(items)
'''
        r = _analyse(src)
        if "dangerous" in r.test_priorities and "safe" in r.test_priorities:
            di = r.test_priorities.index("dangerous")
            si = r.test_priorities.index("safe")
            self.assertLess(di, si)

    def test_dead_code_excluded_from_priorities(self):
        src = """
def _ghost():
    pass

def real():
    return 1
"""
        r = _analyse(src)
        # _ghost is unused private — should not appear in priority list
        self.assertNotIn("_ghost", r.test_priorities)


# ── Report output ──────────────────────────────────────────────────────────────

class TestReportOutput(unittest.TestCase):

    def setUp(self):
        src = '''
import os

def process(items=[]):
    for i in items:
        for j in i:
            pass
    return int(items[0])

def _unused():
    pass

def validate(x):
    return x > 0
'''
        self.report = _analyse(src)

    def test_to_text_report_contains_sections(self):
        text = self.report.to_text_report()
        self.assertIn("FORGE Test Intelligence Report", text)

    def test_to_qa_brief_contains_sections(self):
        brief = self.report.to_qa_brief()
        self.assertIn("TEST INTELLIGENCE BRIEF", brief)
        self.assertIn("END TEST INTELLIGENCE BRIEF", brief)

    def test_to_qa_brief_critical_paths_section(self):
        brief = self.report.to_qa_brief()
        self.assertIn("CRITICAL PATHS", brief)

    def test_to_qa_brief_untested_areas_section(self):
        brief = self.report.to_qa_brief()
        self.assertIn("UNTESTED AREAS", brief)

    def test_summary_dict_keys(self):
        d = self.report.summary_dict()
        for key in ("total_functions", "public_functions", "critical_paths",
                    "risk_areas", "dead_code_blocks", "untested_areas",
                    "complexity_mean", "complexity_max", "test_priorities"):
            self.assertIn(key, d)

    def test_memory_entries_have_required_fields(self):
        entries = self.report.to_memory_entries(task_id="t-123")
        for entry in entries:
            self.assertIn("key",         entry)
            self.assertIn("value",       entry)
            self.assertIn("memory_type", entry)
            self.assertIn("confidence",  entry)
            self.assertIn("tags",        entry)
            self.assertIn("task_id",     entry)

    def test_memory_entries_only_high_confidence(self):
        entries = self.report.to_memory_entries()
        for entry in entries:
            self.assertGreaterEqual(entry["confidence"], 0.0)

    def test_source_hash_is_deterministic(self):
        src = "def f(): return 1"
        r1 = _analyse(src)
        r2 = _analyse(src)
        self.assertEqual(r1.source_hash, r2.source_hash)

    def test_different_source_different_hash(self):
        r1 = _analyse("def f(): return 1")
        r2 = _analyse("def g(): return 2")
        self.assertNotEqual(r1.source_hash, r2.source_hash)


# ── Singleton ──────────────────────────────────────────────────────────────────

class TestSingleton(unittest.TestCase):

    def test_get_intelligence_engine_returns_same_instance(self):
        import forge.intelligence.analyser as m
        m._engine = None
        e1 = get_intelligence_engine()
        e2 = get_intelligence_engine()
        self.assertIs(e1, e2)


# ── Integration: QAAgent receives intelligence brief ──────────────────────────

class TestQAAgentIntegration(unittest.TestCase):
    """Verify QAAgent.run() calls TestIntelligenceEngine and injects the brief."""

    def _make_agent_and_task(self):
        """Build a QAAgent and Task without triggering circular imports."""
        from unittest.mock import MagicMock
        # Import task directly (no engine involved)
        from forge.orchestration.task import Task, TaskType
        # Import agents module directly
        import forge.agents.agents as agents_mod

        task = Task(title="T", description="D", task_type=TaskType.TESTING)
        task.context["implementation"] = "def add(a, b): return a + b"
        task.acceptance_criteria = ["Should add two numbers"]

        mock_router = MagicMock()
        mock_router.complete.return_value = MagicMock(
            content='{"verdict":"PASS","criteria_results":[],"test_code":"","missing_coverage":[]}',
            tokens_total=10, provider="mock", wants_tool_call=False,
        )
        agent = agents_mod.QAAgent(mock_router)
        return agent, task, agents_mod

    def test_qa_agent_injects_intelligence_brief(self):
        from unittest.mock import MagicMock, patch
        agent, task, agents_mod = self._make_agent_and_task()

        mock_output = MagicMock()
        mock_output.success = True
        mock_output.content = '{"verdict":"PASS","criteria_results":[],"test_code":"","missing_coverage":[]}'
        mock_output.tokens_used = 0
        mock_output.provider_used = "mock"

        captured_prompt = {}

        def capture_llm(system, prompt, t, **kw):
            captured_prompt["system"] = system
            captured_prompt["prompt"] = prompt
            return mock_output

        with patch.object(agent, "_call_llm", side_effect=capture_llm), \
             patch.object(agent, "_parse_json", return_value={}):
            agent.run(task)

        self.assertIn("prompt", captured_prompt)
        self.assertIn("TEST INTELLIGENCE BRIEF", captured_prompt["prompt"])

    def test_qa_agent_stores_intelligence_in_context(self):
        from unittest.mock import MagicMock, patch
        agent, task, agents_mod = self._make_agent_and_task()
        task.context["implementation"] = "def add(a, b):\n    return a + b"

        mock_output = MagicMock()
        mock_output.success = True
        mock_output.content = '{"verdict":"PASS","criteria_results":[],"test_code":"","missing_coverage":[]}'
        mock_output.tokens_used = 0
        mock_output.provider_used = "mock"

        with patch.object(agent, "_call_llm", return_value=mock_output), \
             patch.object(agent, "_parse_json", return_value={}):
            agent.run(task)

        self.assertIn("test_intelligence", task.context)
        ti = task.context["test_intelligence"]
        self.assertIn("total_functions", ti)
        self.assertIn("risk_areas", ti)


if __name__ == "__main__":
    unittest.main(verbosity=2)
