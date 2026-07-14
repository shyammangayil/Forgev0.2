"""
End-to-End Tests: CLI
=====================
Tests CLI commands via subprocess. Verifies exit codes and output.
No real API keys required for most tests.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

FORGE_ROOT = str(Path(__file__).parent.parent.parent)
CLI_MODULE = [sys.executable, "-m", "forge.cli"]


def run_cli(*args, cwd=None, env=None):
    """Run forge CLI and return (returncode, stdout, stderr)."""
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        CLI_MODULE + list(args),
        capture_output=True,
        text=True,
        cwd=cwd or FORGE_ROOT,
        env=full_env,
    )
    return result.returncode, result.stdout, result.stderr


class TestCLIHelp(unittest.TestCase):
    def test_help_exits_zero(self):
        code, out, _ = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("FORGE", out)

    def test_version_exits_zero(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn("1.0.0", out)

    def test_run_help(self):
        code, out, _ = run_cli("run", "--help")
        self.assertEqual(code, 0)
        self.assertIn("description", out)

    def test_list_help(self):
        code, out, _ = run_cli("list", "--help")
        self.assertEqual(code, 0)

    def test_health_help(self):
        code, out, _ = run_cli("health", "--help")
        self.assertEqual(code, 0)


class TestCLIHealth(unittest.TestCase):
    def test_health_runs_without_api_keys(self):
        """Health check should complete even with no API keys configured."""
        env = {k: "" for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"]}
        code, out, err = run_cli("health", env=env)
        # Should exit 0 even if no providers are healthy
        self.assertEqual(code, 0)
        self.assertIn("PROVIDER", out)

    def test_health_shows_all_providers(self):
        code, out, _ = run_cli("health")
        for provider in ["ANTHROPIC", "OPENAI", "GEMINI", "OLLAMA"]:
            self.assertIn(provider, out.upper())


class TestCLIList(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_empty_shows_no_tasks_message(self):
        env = {
            "FORGE_TASK_DIR": os.path.join(self.tmpdir, "tasks"),
            "DATABASE_URL": f"sqlite:///{self.tmpdir}/test.db",
            "FORGE_LOG_DIR": os.path.join(self.tmpdir, "logs"),
        }
        code, out, err = run_cli("list", env=env, cwd=FORGE_ROOT)
        self.assertEqual(code, 0)
        # Should show either empty message or a valid tasks table
        self.assertTrue(
            "No tasks" in out or "TASK" in out or "Tasks" in out,
            f"Expected task listing output, got: {out[:200]}"
        )

    def test_list_state_filter_invalid_shows_error(self):
        code, out, err = run_cli("list", "--state", "NONEXISTENT")
        self.assertNotEqual(code, 0)


class TestCLIRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run_creates_task_without_executing(self):
        env = {
            "FORGE_TASK_DIR": os.path.join(self.tmpdir, "tasks"),
            "DATABASE_URL": f"sqlite:///{self.tmpdir}/test.db",
            "FORGE_LOG_DIR": os.path.join(self.tmpdir, "logs"),
        }
        code, out, err = run_cli(
            "run", "Build a hello world function",
            "--dry-run",
            env=env, cwd=FORGE_ROOT,
        )
        self.assertEqual(code, 0)
        self.assertIn("TASK-", out)
        self.assertIn("dry-run", out.lower())

    def test_run_from_file(self):
        task_file = os.path.join(self.tmpdir, "task.txt")
        Path(task_file).write_text("# Simple Task\nBuild a simple function that adds two numbers.")

        env = {
            "FORGE_TASK_DIR": os.path.join(self.tmpdir, "tasks"),
            "DATABASE_URL": f"sqlite:///{self.tmpdir}/test.db",
            "FORGE_LOG_DIR": os.path.join(self.tmpdir, "logs"),
        }
        code, out, err = run_cli(
            "run", "--file", task_file, "--dry-run",
            env=env, cwd=FORGE_ROOT,
        )
        self.assertEqual(code, 0)
        self.assertIn("TASK-", out)

    def test_run_without_description_shows_error(self):
        code, out, err = run_cli("run")
        self.assertNotEqual(code, 0)


class TestCLIMetrics(unittest.TestCase):
    def test_metrics_shows_headers(self):
        code, out, _ = run_cli("metrics")
        self.assertEqual(code, 0)
        # Should show LLM Usage section
        self.assertIn("llm", out.lower())


class TestCLITasks(unittest.TestCase):
    def test_tasks_shows_progress(self):
        """forge tasks reads TASK.md and shows build progress."""
        code, out, _ = run_cli("tasks", cwd=FORGE_ROOT)
        self.assertEqual(code, 0)
        # Should show some progress info
        self.assertTrue(len(out) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
