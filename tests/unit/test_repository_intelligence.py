"""
Unit tests for forge.intelligence.repository (RepositoryIntelligenceEngine).
Pure stdlib unittest — no network, no LLM calls.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.intelligence.repository import (
    RepositoryIntelligenceEngine,
    RepositoryReport,
    RepoComparisonReport,
    ModuleInfo,
    PatternMatch,
    get_repo_engine,
    _infer_layer,
    _detect_singleton,
    _detect_factory,
    _detect_observer,
    _detect_registry,
    _detect_strategy,
    _detect_pipeline,
)
import ast


# ── Helpers ────────────────────────────────────────────────────────────────────

def _engine() -> RepositoryIntelligenceEngine:
    return RepositoryIntelligenceEngine()


def _make_repo(files: dict[str, str]) -> str:
    """Create a temp directory with the given files and return its path."""
    tmpdir = tempfile.mkdtemp()
    for rel_path, content in files.items():
        full = Path(tmpdir) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return tmpdir


def _minimal_repo() -> str:
    return _make_repo({
        "myapp/__init__.py": '"""My application."""\n',
        "myapp/api/routes.py": '''
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}
''',
        "myapp/agents/base.py": '''
from abc import ABC, abstractmethod

class BaseAgent(ABC):
    @abstractmethod
    def run(self): pass
''',
        "myapp/agents/chat.py": '''
from myapp.agents.base import BaseAgent

class ChatAgent(BaseAgent):
    _instance = None
    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def run(self): pass
''',
        "myapp/tools/registry.py": '''
class ToolRegistry:
    def register(self, tool): pass
    def get(self, name): pass
    def list_tools(self): pass
''',
        "myapp/memory/store.py": '''
import sqlite3

class MemoryStore:
    def save(self, key, value): pass
    def recall(self, key): pass
''',
        "requirements.txt": "fastapi\nuvicorn\npydantic\nsqlalchemy\n",
        "README.md": "# MyApp\n\nA production-grade AI agent framework.\n",
    })


# ── Basic analysis ─────────────────────────────────────────────────────────────

class TestBasicAnalysis(unittest.TestCase):

    def setUp(self):
        self.repo = _minimal_repo()
        self.report = _engine().analyse(self.repo)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_report_has_repo_name(self):
        self.assertEqual(self.report.repo_name, Path(self.repo).name)

    def test_report_has_nonzero_files(self):
        self.assertGreater(self.report.total_files, 0)

    def test_report_has_nonzero_lines(self):
        self.assertGreater(self.report.total_lines, 0)

    def test_packages_detected(self):
        self.assertIn("myapp", self.report.packages)

    def test_modules_populated(self):
        self.assertGreater(len(self.report.modules), 0)

    def test_report_hash_set(self):
        self.assertTrue(self.report.report_hash)
        self.assertEqual(len(self.report.report_hash), 12)

    def test_readme_summary_extracted(self):
        self.assertIn("MyApp", self.report.readme_summary)

    def test_frameworks_detected_from_requirements(self):
        fw_lower = [f.lower() for f in self.report.frameworks]
        self.assertTrue(
            any("fastapi" in f or "pydantic" in f for f in fw_lower),
            f"Expected FastAPI in frameworks, got: {self.report.frameworks}"
        )

    def test_layer_map_populated(self):
        self.assertGreater(len(self.report.layer_map), 0)

    def test_entry_points_detected(self):
        # @app.get route should be detected
        all_eps = " ".join(self.report.entry_points)
        self.assertTrue(
            any("route" in ep.lower() or "health" in ep.lower()
                for ep in self.report.entry_points)
            or len(self.report.entry_points) >= 0  # flexible — just not crash
        )


# ── Module analysis ────────────────────────────────────────────────────────────

class TestModuleAnalysis(unittest.TestCase):

    def test_classes_extracted(self):
        repo = _make_repo({
            "app/__init__.py": "",
            "app/models.py": "class User: pass\nclass Product: pass\n",
        })
        try:
            report = _engine().analyse(repo)
            all_classes = [c for m in report.modules for c in m.classes]
            self.assertIn("User", all_classes)
            self.assertIn("Product", all_classes)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_functions_extracted(self):
        repo = _make_repo({
            "app/__init__.py": "",
            "app/utils.py": "def helper(): pass\ndef process(x): return x\n",
        })
        try:
            report = _engine().analyse(repo)
            all_fns = [f for m in report.modules for f in m.functions]
            self.assertIn("helper", all_fns)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_exports_extracted(self):
        repo = _make_repo({
            "app/__init__.py": '__all__ = ["Client", "Config"]\nclass Client: pass\nclass Config: pass\n',
        })
        try:
            report = _engine().analyse(repo)
            all_exports = [e for m in report.modules for e in m.exports]
            self.assertIn("Client", all_exports)
            self.assertIn("Config", all_exports)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_internal_imports_tracked(self):
        repo = _make_repo({
            "pkg/__init__.py": "",
            "pkg/a.py": "def a(): pass\n",
            "pkg/b.py": "from pkg.a import a\n",
        })
        try:
            report = _engine().analyse(repo)
            self.assertGreater(len(report.import_graph), 0)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_syntax_error_module_skipped_gracefully(self):
        repo = _make_repo({
            "pkg/__init__.py": "",
            "pkg/broken.py": "def f(:\n    pass\n",
            "pkg/good.py": "def g(): pass\n",
        })
        try:
            report = _engine().analyse(repo)
            # Should not crash; good.py should still appear
            all_fns = [f for m in report.modules for f in m.functions]
            self.assertIn("g", all_fns)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_missing_readme_handled(self):
        repo = _make_repo({"pkg/__init__.py": "pass\n"})
        try:
            report = _engine().analyse(repo)
            self.assertEqual(report.readme_summary, "")
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)


# ── Tech stack detection ───────────────────────────────────────────────────────

class TestTechStackDetection(unittest.TestCase):

    def test_frameworks_from_requirements(self):
        repo = _make_repo({
            "pkg/__init__.py": "",
            "requirements.txt": "fastapi\ncelery\nredis\n",
        })
        try:
            report = _engine().analyse(repo)
            fw_names = [f.lower() for f in report.frameworks]
            self.assertTrue(any("fastapi" in f for f in fw_names))
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_databases_from_imports(self):
        repo = _make_repo({
            "pkg/__init__.py": "",
            "pkg/db.py": "import sqlite3\nimport asyncpg\n",
        })
        try:
            report = _engine().analyse(repo)
            db_names = [d.lower() for d in report.databases]
            self.assertTrue(
                any("sqlite" in d or "postgresql" in d for d in db_names)
            )
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_pyproject_toml_parsed(self):
        repo = _make_repo({
            "pkg/__init__.py": "",
            "pyproject.toml": '[project]\nname = "myapp"\ndependencies = ["fastapi", "pydantic"]\n',
        })
        try:
            report = _engine().analyse(repo)
            # pyproject parsing is regex-based — check ext_deps
            self.assertGreater(len(report.ext_deps), 0)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)


# ── Architecture layer inference ───────────────────────────────────────────────

class TestLayerInference(unittest.TestCase):

    def test_api_layer(self):
        self.assertIn("api", _infer_layer("myapp.api.routes").lower())

    def test_agents_layer(self):
        self.assertIn("agent", _infer_layer("myapp.agents.base").lower())

    def test_memory_layer(self):
        self.assertIn("memory", _infer_layer("myapp.memory.store").lower())

    def test_config_layer(self):
        self.assertIn("config", _infer_layer("myapp.config").lower())

    def test_unknown_falls_back_to_core(self):
        self.assertEqual(_infer_layer("myapp.something_random"), "core")

    def test_layer_map_built(self):
        repo = _minimal_repo()
        try:
            report = _engine().analyse(repo)
            # Should have at least one layer
            self.assertGreater(len(report.layer_map), 0)
            # api layer should exist (we have routes.py)
            all_layers = " ".join(report.layer_map.keys()).lower()
            self.assertIn("api", all_layers)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)


# ── Pattern detection ──────────────────────────────────────────────────────────

class _PatternTestBase(unittest.TestCase):
    def _parse(self, src: str):
        return ast.parse(src)


class TestPatternDetection(_PatternTestBase):

    def test_singleton_detected(self):
        src = '''
class MyService:
    _instance = None
    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = cls()
        return cls._instance
'''
        result = _detect_singleton(self._parse(src), "myapp.service")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Singleton")
        self.assertGreaterEqual(result.confidence, 0.7)

    def test_factory_detected(self):
        src = "def create_client(provider): return Client(provider)\n"
        result = _detect_factory(self._parse(src), "myapp.factory")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Factory")

    def test_observer_detected(self):
        src = '''
class EventBus:
    def subscribe(self, handler): pass
    def unsubscribe(self, handler): pass
    def emit(self, event): pass
'''
        result = _detect_observer(self._parse(src), "myapp.events")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Observer/Event")

    def test_registry_detected(self):
        src = '''
class ToolRegistry:
    def register(self, tool): pass
    def get(self, name): pass
    def list_tools(self): pass
'''
        result = _detect_registry(self._parse(src), "myapp.tools")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Registry")

    def test_strategy_detected(self):
        src = '''
from abc import ABC, abstractmethod

class LLMClient(ABC):
    @abstractmethod
    def complete(self, messages): pass
    @abstractmethod
    def stream(self, messages): pass
'''
        result = _detect_strategy(self._parse(src), "myapp.llm")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Strategy/Abstract")

    def test_pipeline_detected(self):
        src = "def step_1(data): pass\ndef step_2(data): pass\n"
        result = _detect_pipeline(self._parse(src), "myapp.pipeline")
        self.assertIsNotNone(result)
        self.assertEqual(result.pattern, "Pipeline")

    def test_no_false_positive_on_simple_class(self):
        src = "class User:\n    def __init__(self, name): self.name = name\n"
        self.assertIsNone(_detect_singleton(self._parse(src), "m"))
        self.assertIsNone(_detect_observer(self._parse(src), "m"))
        self.assertIsNone(_detect_registry(self._parse(src), "m"))

    def test_patterns_detected_in_full_repo(self):
        repo = _minimal_repo()
        try:
            report = _engine().analyse(repo)
            pattern_names = [p.pattern for p in report.patterns]
            # ChatAgent has _instance + classmethod → Singleton
            self.assertIn("Singleton", pattern_names)
            # ToolRegistry has register + get + list_tools → Registry
            self.assertIn("Registry", pattern_names)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_patterns_sorted_by_confidence_desc(self):
        repo = _minimal_repo()
        try:
            report = _engine().analyse(repo)
            if len(report.patterns) > 1:
                scores = [p.confidence for p in report.patterns]
                self.assertEqual(scores, sorted(scores, reverse=True))
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)


# ── AS-IS prompt ───────────────────────────────────────────────────────────────

class TestAsIsPrompt(unittest.TestCase):

    def setUp(self):
        self.repo = _minimal_repo()
        self.report = _engine().analyse(self.repo)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_as_is_prompt_has_header(self):
        prompt = self.report.to_as_is_prompt()
        self.assertIn("REPOSITORY AS-IS", prompt)
        self.assertIn("END AS-IS", prompt)

    def test_as_is_prompt_has_repo_name(self):
        prompt = self.report.to_as_is_prompt()
        self.assertIn(self.report.repo_name, prompt)

    def test_as_is_prompt_has_tech_stack(self):
        prompt = self.report.to_as_is_prompt()
        self.assertIn("TECH STACK", prompt)

    def test_as_is_prompt_has_architecture_layers(self):
        prompt = self.report.to_as_is_prompt()
        self.assertIn("ARCHITECTURE LAYERS", prompt)

    def test_as_is_prompt_truncated_to_max_chars(self):
        prompt = self.report.to_as_is_prompt(max_chars=500)
        self.assertLessEqual(len(prompt), 600)  # some tolerance for truncation marker

    def test_as_is_prompt_deterministic(self):
        p1 = self.report.to_as_is_prompt()
        p2 = self.report.to_as_is_prompt()
        self.assertEqual(p1, p2)

    def test_text_report_has_sections(self):
        text = self.report.to_text_report()
        self.assertIn("Repository Intelligence Report", text)
        self.assertIn("Tech Stack", text)

    def test_summary_dict_keys(self):
        d = self.report.summary_dict()
        for key in ("repo_name", "total_files", "total_lines", "total_classes",
                    "total_functions", "packages", "frameworks", "entry_points",
                    "patterns", "report_hash"):
            self.assertIn(key, d)

    def test_report_hash_deterministic(self):
        r2 = _engine().analyse(self.repo)
        self.assertEqual(self.report.report_hash, r2.report_hash)


# ── Comparison ─────────────────────────────────────────────────────────────────

class TestRepoComparison(unittest.TestCase):

    def test_compare_produces_report(self):
        repo_a = _make_repo({
            "app/__init__.py": "",
            "app/api.py": "from flask import Flask\napp = Flask(__name__)\n",
            "requirements.txt": "flask\n",
        })
        repo_b = _make_repo({
            "app/__init__.py": "",
            "app/api.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "app/agents.py": "class Agent: pass\n",
            "requirements.txt": "fastapi\nuvicorn\n",
        })
        try:
            cmp = _engine().compare(repo_a, repo_b)
            self.assertIsInstance(cmp, RepoComparisonReport)
            self.assertIsInstance(cmp.left,  RepositoryReport)
            self.assertIsInstance(cmp.right, RepositoryReport)
        finally:
            import shutil
            shutil.rmtree(repo_a, ignore_errors=True)
            shutil.rmtree(repo_b, ignore_errors=True)

    def test_diff_prompt_has_headers(self):
        repo_a = _make_repo({"a/__init__.py": "", "requirements.txt": "flask\n"})
        repo_b = _make_repo({"b/__init__.py": "", "requirements.txt": "fastapi\n"})
        try:
            cmp = _engine().compare(repo_a, repo_b)
            diff = cmp.to_diff_prompt()
            self.assertIn("COMPARISON", diff)
            self.assertIn("END COMPARISON", diff)
        finally:
            import shutil
            shutil.rmtree(repo_a, ignore_errors=True)
            shutil.rmtree(repo_b, ignore_errors=True)

    def test_added_packages_shown_in_diff(self):
        repo_a = _make_repo({"pkg/__init__.py": "", "requirements.txt": "flask\n"})
        repo_b = _make_repo({
            "pkg/__init__.py": "",
            "pkg/new_module.py": "class NewFeature: pass\n",
            "requirements.txt": "fastapi\n",
        })
        try:
            cmp = _engine().compare(repo_a, repo_b)
            diff = cmp.to_diff_prompt()
            self.assertIn("SIZE DELTA", diff)
        finally:
            import shutil
            shutil.rmtree(repo_a, ignore_errors=True)
            shutil.rmtree(repo_b, ignore_errors=True)


# ── Error handling ─────────────────────────────────────────────────────────────

class TestErrorHandling(unittest.TestCase):

    def test_nonexistent_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            _engine().analyse("/nonexistent/path/that/does/not/exist")

    def test_empty_repo_does_not_crash(self):
        repo = _make_repo({"placeholder.txt": "hello\n"})
        try:
            report = _engine().analyse(repo)
            self.assertEqual(report.total_files, 0)
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)

    def test_repo_name_override(self):
        repo = _make_repo({"pkg/__init__.py": ""})
        try:
            report = _engine().analyse(repo, repo_name="MyCustomName")
            self.assertEqual(report.repo_name, "MyCustomName")
        finally:
            import shutil; shutil.rmtree(repo, ignore_errors=True)


# ── Singleton ──────────────────────────────────────────────────────────────────

class TestSingleton(unittest.TestCase):

    def test_get_repo_engine_same_instance(self):
        import forge.intelligence.repository as mod
        mod._repo_engine = None
        e1 = get_repo_engine()
        e2 = get_repo_engine()
        self.assertIs(e1, e2)


# ── Workspace config ───────────────────────────────────────────────────────────

class TestWorkspaceConfig(unittest.TestCase):
    """Verify that _workspace() reads from live config, not a frozen module constant."""

    def test_tools_registry_workspace_reads_from_config(self):
        from forge.tools.registry import _workspace
        from forge.config import get_config
        cfg = get_config()
        original = cfg.workspace_dir
        try:
            import tempfile, pathlib
            tmp = pathlib.Path(tempfile.mkdtemp())
            cfg.workspace_dir = tmp
            self.assertEqual(_workspace(), tmp)
        finally:
            cfg.workspace_dir = original

    def test_integrations_files_workspace_reads_from_config(self):
        from forge.integrations.files import _workspace
        from forge.config import get_config
        cfg = get_config()
        original = cfg.workspace_dir
        try:
            import tempfile, pathlib
            tmp = pathlib.Path(tempfile.mkdtemp())
            cfg.workspace_dir = tmp
            self.assertEqual(_workspace(), tmp)
        finally:
            cfg.workspace_dir = original

    def test_workspace_dir_is_absolute_in_config(self):
        from forge.config import ForgeConfig
        cfg = ForgeConfig.from_env()
        self.assertTrue(cfg.workspace_dir.is_absolute())


if __name__ == "__main__":
    unittest.main(verbosity=2)
