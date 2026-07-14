"""
Unit tests for Forge Thunderbird local stack.
Pure stdlib unittest — no LLM calls, no network, no llama.cpp required.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── ModelRegistry ──────────────────────────────────────────────────────────────

class TestModelRegistry(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_gguf(self, name: str, subdir: str = "") -> Path:
        """Create a zero-byte .gguf file for testing."""
        base = Path(self.tmpdir) / subdir if subdir else Path(self.tmpdir)
        base.mkdir(parents=True, exist_ok=True)
        p = base / name
        p.write_bytes(b"\x00" * 1024)   # 1 KB placeholder
        return p

    def _registry(self):
        from forge.local_stack.model_registry import ModelRegistry
        return ModelRegistry(gpu_layers=99, threads=8, context_len=4096)

    def test_scan_empty_dir_returns_empty(self):
        r = self._registry()
        profiles = r.scan([self.tmpdir])
        self.assertEqual(profiles, {})

    def test_detects_coding_model(self):
        self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertIn("coding", r.profiles)
        self.assertEqual(r.profiles["coding"].alias, "qwen3")

    def test_detects_reasoning_model(self):
        self._make_gguf("reasoning-DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertIn("reasoning", r.profiles)
        self.assertEqual(r.profiles["reasoning"].alias, "deepseek")

    def test_detects_vision_model(self):
        self._make_gguf("language-Qwen3VL-8B-Instruct-Q4_K_M.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertIn("vision", r.profiles)

    def test_detects_mmproj_and_associates_with_vision(self):
        self._make_gguf("language-Qwen3VL-8B-Instruct-Q4_K_M.gguf")
        self._make_gguf("vision-mmproj-Qwen3VL-8B-Instruct-F16.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertIn("vision", r.profiles)
        self.assertTrue(r.profiles["vision"].mmproj_path,
                        "mmproj_path should be set for vision model")
        self.assertIn("mmproj", r.profiles["vision"].mmproj_path.lower())

    def test_mmproj_not_added_as_standalone_profile(self):
        self._make_gguf("vision-mmproj-Qwen3VL-8B-Instruct-F16.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertNotIn("mmproj", r.profiles)

    def test_scan_multiple_dirs(self):
        tmp2 = tempfile.mkdtemp()
        try:
            self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf")
            Path(tmp2, "reasoning-DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf").write_bytes(b"\x00")
            r = self._registry()
            r.scan([self.tmpdir, tmp2])
            self.assertIn("coding",    r.profiles)
            self.assertIn("reasoning", r.profiles)
        finally:
            import shutil; shutil.rmtree(tmp2, ignore_errors=True)

    def test_duplicate_role_keeps_first_discovered(self):
        self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf", "dir_a")
        self._make_gguf("coding-another-model.gguf",    "dir_b")
        r = self._registry()
        r.scan([self.tmpdir])
        # Only one coding profile
        self.assertEqual(len([p for p in r.profiles if p == "coding"]), 1)

    def test_env_override_replaces_scanned_profile(self):
        self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf")
        override_file = Path(self.tmpdir) / "override-coding.gguf"
        override_file.write_bytes(b"\x00" * 2048)
        r = self._registry()
        r.scan([self.tmpdir])
        with patch.dict(os.environ,
                        {"FORGE_CODING_MODEL_PATH": str(override_file)}):
            r.apply_env_overrides()
        self.assertEqual(r.profiles["coding"].model_path, str(override_file))

    def test_nonexistent_dir_skipped_gracefully(self):
        r = self._registry()
        profiles = r.scan(["/nonexistent/path/that/does/not/exist"])
        self.assertEqual(profiles, {})

    def test_summary_output_not_empty(self):
        self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        summary = r.summary()
        self.assertIn("coding", summary)
        self.assertIn("qwen3",  summary)

    def test_get_profile_returns_correct(self):
        self._make_gguf("coding-Qwen3-8B-Q4_K_M.gguf")
        r = self._registry()
        r.scan([self.tmpdir])
        p = r.get_profile("coding")
        self.assertIsNotNone(p)
        self.assertEqual(p.role, "coding")

    def test_get_profile_missing_returns_none(self):
        r = self._registry()
        r.scan([self.tmpdir])
        self.assertIsNone(r.get_profile("vision"))

    def test_get_alias_returns_default_for_missing(self):
        r = self._registry()
        r.scan([])
        self.assertEqual(r.get_alias("coding"),    "qwen3")
        self.assertEqual(r.get_alias("reasoning"), "deepseek")


# ── Role detection heuristics ──────────────────────────────────────────────────

class TestRoleDetection(unittest.TestCase):

    def setUp(self):
        from forge.local_stack.model_registry import ModelRegistry
        self.r = ModelRegistry()

    def test_coding_detection(self):
        self.assertEqual(self.r._detect_role("coding-Qwen3-8B-Q4_K_M"),   "coding")
        self.assertEqual(self.r._detect_role("Qwen3-8B-Q4_K_M"),          "coding")
        self.assertEqual(self.r._detect_role("CodeLlama-7B"),              "coding")

    def test_reasoning_detection(self):
        self.assertEqual(self.r._detect_role("DeepSeek-R1-Distill-Qwen"), "reasoning")
        self.assertEqual(self.r._detect_role("reasoning-model"),           "reasoning")

    def test_vision_detection(self):
        self.assertEqual(self.r._detect_role("Qwen3VL-8B"),         "vision")
        self.assertEqual(self.r._detect_role("qwen3vl-instruct"),   "vision")

    def test_mmproj_detection(self):
        self.assertEqual(self.r._detect_role("mmproj-Qwen3VL-F16"), "mmproj")
        self.assertEqual(self.r._detect_role("vision-mmproj-something"), "mmproj")

    def test_custom_fallback(self):
        self.assertEqual(self.r._detect_role("unknown-random-model"), "custom")


# ── Config Generator ───────────────────────────────────────────────────────────

class TestConfigGenerator(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from forge.local_stack.model_registry import ModelRegistry, ModelProfile
        self.registry = ModelRegistry()
        self.registry.profiles = {
            "coding": ModelProfile(
                role="coding", alias="qwen3",
                model_path="D:/models/coding-Qwen3-8B-Q4_K_M.gguf",
                size_gb=4.5,
            ),
            "reasoning": ModelProfile(
                role="reasoning", alias="deepseek",
                model_path="E:/models-IMP/reasoning-DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
                size_gb=8.9,
            ),
            "vision": ModelProfile(
                role="vision", alias="qwen3vl",
                model_path="D:/models/language-Qwen3VL-8B-Instruct-Q4_K_M.gguf",
                mmproj_path="D:/models/vision-mmproj-Qwen3VL-8B-Instruct-F16.gguf",
                size_gb=5.1,
            ),
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_llama_swap_config_written(self):
        from forge.local_stack.config_generator import generate_llama_swap_config
        out = str(Path(self.tmpdir) / "llama_swap.yaml")
        content = generate_llama_swap_config(
            self.registry, out, "llama-server.exe",
            llama_server_port=8081, llama_swap_port=8080,
        )
        self.assertTrue(Path(out).exists())
        self.assertIn("qwen3",    content)
        self.assertIn("deepseek", content)
        self.assertIn("qwen3vl",  content)
        self.assertIn("8081",     content)
        self.assertIn("8080",     content)

    def test_llama_swap_config_contains_mmproj(self):
        from forge.local_stack.config_generator import generate_llama_swap_config
        out = str(Path(self.tmpdir) / "llama_swap.yaml")
        content = generate_llama_swap_config(
            self.registry, out, "llama-server.exe",
        )
        self.assertIn("mmproj", content)

    def test_litellm_config_written(self):
        from forge.local_stack.config_generator import generate_litellm_config
        out = str(Path(self.tmpdir) / "litellm.yaml")
        content = generate_litellm_config(
            self.registry, out,
            llama_swap_port=8080, litellm_port=4000,
        )
        self.assertTrue(Path(out).exists())
        self.assertIn("qwen3",    content)
        self.assertIn("deepseek", content)
        self.assertIn("4000",     content)
        self.assertIn("8080",     content)

    def test_litellm_config_cloud_entry_added_when_key_present(self):
        from forge.local_stack.config_generator import generate_litellm_config
        out = str(Path(self.tmpdir) / "litellm_cloud.yaml")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            content = generate_litellm_config(self.registry, out)
        self.assertIn("anthropic", content.lower())
        self.assertIn("claude",    content)

    def test_generate_all_creates_both_files(self):
        from forge.local_stack.config_generator import generate_all
        cfg = MagicMock()
        cfg.llama_cpp_binary  = "llama-server.exe"
        cfg.llama_swap_config = "swap.yaml"
        cfg.litellm_config    = "litellm.yaml"
        cfg.llama_server_port = 8081
        cfg.llama_swap_port   = 8080
        cfg.litellm_port      = 4000
        paths = generate_all(self.registry, cfg, self.tmpdir)
        self.assertTrue(Path(paths["llama_swap_config"]).exists())
        self.assertTrue(Path(paths["litellm_config"]).exists())


# ── LiteLLMProvider ────────────────────────────────────────────────────────────

class TestLiteLLMProvider(unittest.TestCase):

    def _make_provider(self):
        from forge.providers.base import LiteLLMProvider
        return LiteLLMProvider(
            api_key  = "",
            base_url = "http://localhost:4000",
            timeout  = 30,
        )

    def test_provider_name(self):
        from forge.providers.base import LiteLLMProvider
        self.assertEqual(LiteLLMProvider.name, "litellm")

    def test_provider_in_registry(self):
        from forge.providers.base import PROVIDER_CLASSES
        self.assertIn("litellm", PROVIDER_CLASSES)

    def test_zero_cost(self):
        from forge.providers.base import CompletionResponse
        resp = CompletionResponse(
            content="hi", model="qwen3", provider="litellm",
            tokens_input=100, tokens_output=50, duration_ms=200,
        )
        self.assertEqual(resp.estimated_cost_usd, 0.0)

    def test_build_provider_litellm(self):
        from forge.providers.base import build_provider
        p = build_provider("litellm", api_key="", base_url="http://localhost:4000")
        self.assertEqual(p.name, "litellm")

    def test_health_check_returns_false_when_server_not_running(self):
        p = self._make_provider()
        # No server running — should return False, not raise
        result = p.health_check()
        self.assertFalse(result)

    def test_list_models_returns_empty_when_server_not_running(self):
        p = self._make_provider()
        result = p.list_models()
        self.assertEqual(result, [])


# ── Config ─────────────────────────────────────────────────────────────────────

class TestLocalStackConfig(unittest.TestCase):

    def test_local_stack_config_defaults(self):
        from forge.config import LocalStackConfig
        cfg = LocalStackConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.llama_swap_port, 8080)
        self.assertEqual(cfg.litellm_port, 4000)
        self.assertEqual(cfg.llama_server_port, 8081)
        self.assertEqual(cfg.gpu_layers, 99)
        self.assertEqual(cfg.threads, 8)

    def test_model_profile_defaults(self):
        from forge.config import ModelProfile
        p = ModelProfile(role="coding", alias="qwen3", model_path="/models/q.gguf")
        self.assertEqual(p.context_len, 4096)
        self.assertEqual(p.gpu_layers, 99)
        self.assertEqual(p.mmproj_path, "")

    def test_from_env_reads_local_stack_vars(self):
        from forge.config import ForgeConfig
        env = {
            "FORGE_LOCAL_STACK_ENABLED": "true",
            "FORGE_LLAMA_SWAP_PORT":     "9090",
            "FORGE_LITELLM_PORT":        "5000",
            "FORGE_GPU_LAYERS":          "50",
            "FORGE_THREADS":             "4",
            "FORGE_MODELS_DIR":          "D:/models;E:/models-IMP",
            "FORGE_CODING_MODEL":        "myqwen",
            "FORGE_REASONING_MODEL":     "mydeepseek",
        }
        with patch.dict(os.environ, env):
            cfg = ForgeConfig.from_env()
        self.assertTrue(cfg.local_stack.enabled)
        self.assertEqual(cfg.local_stack.llama_swap_port, 9090)
        self.assertEqual(cfg.local_stack.litellm_port, 5000)
        self.assertEqual(cfg.local_stack.gpu_layers, 50)
        self.assertEqual(cfg.local_stack.threads, 4)
        self.assertEqual(cfg.router.coding_model, "myqwen")
        self.assertEqual(cfg.router.reasoning_model, "mydeepseek")
        self.assertIn("D:/models", cfg.local_stack.models_dirs)

    def test_litellm_prepended_to_fallback_when_enabled(self):
        from forge.config import ForgeConfig
        with patch.dict(os.environ, {"FORGE_LOCAL_STACK_ENABLED": "true"}):
            cfg = ForgeConfig.from_env()
        self.assertEqual(cfg.router.fallback_chain[0], "litellm")
        self.assertEqual(cfg.router.coding_provider, "litellm")

    def test_litellm_not_in_chain_when_disabled(self):
        from forge.config import ForgeConfig
        with patch.dict(os.environ, {"FORGE_LOCAL_STACK_ENABLED": "false"}):
            cfg = ForgeConfig.from_env()
        # litellm should not be first when disabled
        self.assertNotEqual(cfg.router.fallback_chain[0], "litellm")


# ── Router alias resolution ────────────────────────────────────────────────────

class TestRouterAliasResolution(unittest.TestCase):

    def _make_router(self, coding_model="qwen3", reasoning_model="deepseek"):
        from forge.config import ForgeConfig, RouterConfig
        from forge.router.model_router import ModelRouter
        with patch.dict(os.environ, {
            "FORGE_LOCAL_STACK_ENABLED": "true",
            "FORGE_CODING_MODEL":        coding_model,
            "FORGE_REASONING_MODEL":     reasoning_model,
        }):
            cfg = ForgeConfig.from_env()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = {}
        router._learned_preferences = {}
        return router

    def test_coding_task_gets_coding_alias(self):
        from forge.orchestration.task import TaskType
        router = self._make_router()
        alias  = router._default_model_for("litellm", TaskType.CODING)
        self.assertEqual(alias, "qwen3")

    def test_planning_task_gets_reasoning_alias(self):
        from forge.orchestration.task import TaskType
        router = self._make_router()
        alias  = router._default_model_for("litellm", TaskType.PLANNING)
        self.assertEqual(alias, "deepseek")

    def test_analysis_task_gets_reasoning_alias(self):
        from forge.orchestration.task import TaskType
        router = self._make_router()
        alias  = router._default_model_for("litellm", TaskType.ANALYSIS)
        self.assertEqual(alias, "deepseek")

    def test_explicit_litellm_model_override_wins(self):
        from forge.orchestration.task import TaskType
        from forge.config import ForgeConfig
        from forge.router.model_router import ModelRouter
        with patch.dict(os.environ, {
            "FORGE_LOCAL_STACK_ENABLED": "true",
        }):
            cfg = ForgeConfig.from_env()
        cfg.router.litellm_model = "custom-alias"
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = {}
        router._learned_preferences = {}
        alias = router._default_model_for("litellm", TaskType.CODING)
        self.assertEqual(alias, "custom-alias")


# ── Health ─────────────────────────────────────────────────────────────────────

class TestLocalStackHealth(unittest.TestCase):

    def test_check_litellm_returns_false_when_not_running(self):
        from forge.local_stack.health import LocalStackHealth
        h = LocalStackHealth(litellm_port=19999)  # nothing on this port
        status = h.check_litellm()
        self.assertFalse(status.running)
        self.assertNotEqual(status.error, "")

    def test_check_llama_swap_returns_false_when_not_running(self):
        from forge.local_stack.health import LocalStackHealth
        h = LocalStackHealth(llama_swap_port=19998)
        status = h.check_llama_swap()
        self.assertFalse(status.running)

    def test_full_report_returns_all_components(self):
        from forge.local_stack.health import LocalStackHealth
        h = LocalStackHealth(
            llama_swap_port=19997,
            litellm_port=19996,
            llama_server_port=19995,
        )
        report = h.full_report()
        self.assertIn("litellm",      report)
        self.assertIn("llama_swap",   report)
        self.assertIn("llama_server", report)

    def test_all_healthy_false_when_nothing_running(self):
        from forge.local_stack.health import LocalStackHealth
        h = LocalStackHealth(
            llama_swap_port=19994,
            litellm_port=19993,
            llama_server_port=19992,
        )
        self.assertFalse(h.all_healthy())

    def test_format_report_contains_component_names(self):
        from forge.local_stack.health import LocalStackHealth
        h = LocalStackHealth(
            llama_swap_port=19991,
            litellm_port=19990,
            llama_server_port=19989,
        )
        report = h.format_report()
        self.assertIn("LiteLLM",    report)
        self.assertIn("llama-swap", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
