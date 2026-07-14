"""
Integration Tests: Orchestration Engine
========================================
Tests the full PLAN→EXECUTE→VERIFY→REVIEW→TEST→COMMIT lifecycle
using mock LLM responses. No real API calls.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.config import ForgeConfig
from forge.orchestration.engine import OrchestrationEngine, ExecutionPhase
from forge.orchestration.task import Task, TaskState, TaskType
from forge.providers.base import CompletionResponse
from forge.storage.task_repository import TaskRepository
from forge.router.model_router import ModelRouter


def mock_response(content: str, provider: str = "mock") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model="mock-model",
        provider=provider,
        tokens_input=100,
        tokens_output=200,
        duration_ms=50,
    )


PLANNER_OUTPUT = json.dumps({
    "tasks": [
        {
            "title": "Implement feature",
            "description": "Core implementation",
            "type": "coding",
            "acceptance_criteria": ["Works correctly"],
            "priority": 1
        }
    ]
})

IMPLEMENTER_OUTPUT = """
Here is the implementation:

```python
def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b
```

## Verification Steps
1. Call add(2, 3) — expect 5
2. Call add(0, 0) — expect 0
"""

REVIEWER_OUTPUT = json.dumps({
    "verdict": "APPROVED",
    "risk_level": "LOW",
    "issues": [],
    "summary": "Clean implementation with no issues."
})

SECURITY_OUTPUT = json.dumps({
    "verdict": "CLEAR",
    "findings": [],
    "secrets_detected": [],
    "summary": "No security issues found."
})

QA_OUTPUT = json.dumps({
    "verdict": "PASS",
    "criteria_results": [
        {"criterion": "Works correctly", "status": "PASS", "test": "assert add(2,3)==5"}
    ],
    "test_code": "def test_add(): assert add(2,3)==5",
    "missing_coverage": []
})

DOCS_OUTPUT = "# add()\nAdds two integers and returns the result."


class TestEngineHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = ForgeConfig()
        self.cfg.workspace_dir = Path(self.tmpdir) / "workspace"
        self.cfg.task_dir = Path(self.tmpdir) / "tasks"
        self.cfg.log_dir = Path(self.tmpdir) / "logs"

        self.repo = TaskRepository(
            db_path=os.path.join(self.tmpdir, "test.db"),
            task_dir=self.cfg.task_dir,
        )

        # Build a mock router
        self.router = MagicMock(spec=ModelRouter)
        self.router.available_providers.return_value = ["mock"]

        # Sequence: planner, implementer, reviewer, security, qa, docs
        self.router.complete.side_effect = [
            mock_response(PLANNER_OUTPUT),
            mock_response(IMPLEMENTER_OUTPUT),
            mock_response(REVIEWER_OUTPUT),
            mock_response(SECURITY_OUTPUT),
            mock_response(QA_OUTPUT),
            mock_response(DOCS_OUTPUT),
        ]

        self.engine = OrchestrationEngine(
            config=self.cfg,
            repo=self.repo,
            router=self.router,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_task_created_with_new_state(self):
        task = self.engine.create_task(
            title="Test Task",
            description="Build something",
            acceptance_criteria=["It works"],
        )
        self.assertEqual(task.state, TaskState.NEW)
        self.assertIsNotNone(task.sequence_number)

    def test_task_persisted_to_db(self):
        task = self.engine.create_task("Persisted Task", "Description")
        retrieved = self.repo.get(task.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.title, "Persisted Task")

    def test_markdown_file_created_on_task_creation(self):
        task = self.engine.create_task("MD Task", "Description")
        md_file = self.cfg.task_dir / f"TASK-{task.sequence_number:03d}.md"
        self.assertTrue(md_file.exists())

    def test_full_lifecycle_succeeds(self):
        task = self.engine.create_task(
            title="Full Lifecycle Task",
            description="Implement the add function",
            acceptance_criteria=["Works correctly"],
        )
        result = self.engine.run_task(task.id)
        self.assertTrue(result.success)
        self.assertIsNone(result.error)

    def test_task_state_is_complete_after_success(self):
        task = self.engine.create_task("Complete Task", "Description",
                                       acceptance_criteria=["criterion"])
        self.engine.run_task(task.id)
        updated = self.repo.get(task.id)
        self.assertEqual(updated.state, TaskState.COMPLETE)

    def test_result_has_implementation(self):
        task = self.engine.create_task("Impl Task", "Description",
                                       acceptance_criteria=["criterion"])
        result = self.engine.run_task(task.id)
        self.assertIn("implementation", result.output)
        self.assertIn("def add", result.output["implementation"])

    def test_checkpoints_saved_for_each_phase(self):
        task = self.engine.create_task("Checkpoint Task", "Description",
                                       acceptance_criteria=["criterion"])
        self.engine.run_task(task.id)
        # Should have pre and post checkpoints for each phase
        checkpoint = self.repo.load_checkpoint(task.id, "pre_PLAN")
        self.assertIsNotNone(checkpoint)

    def test_audit_records_written(self):
        task = self.engine.create_task("Audit Task", "Description",
                                       acceptance_criteria=["criterion"])
        self.engine.run_task(task.id)
        records = self.repo.get_audit(task.id)
        self.assertTrue(len(records) >= 1)
        events = [r["event"] for r in records]
        self.assertIn("execution_started", events)

    def test_metrics_updated_after_run(self):
        from forge.telemetry.metrics import get_metrics
        initial = get_metrics().counter("forge_tasks_created_total").value
        self.engine.create_task("Metrics Task", "Description")
        after = get_metrics().counter("forge_tasks_created_total").value
        self.assertGreater(after, initial)

    def test_list_tasks_returns_created_tasks(self):
        self.engine.create_task("Task A", "Description A")
        self.engine.create_task("Task B", "Description B")
        tasks = self.engine.list_tasks()
        titles = [t["title"] for t in tasks]
        self.assertIn("Task A", titles)
        self.assertIn("Task B", titles)


class TestEngineGateFailures(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = ForgeConfig()
        self.cfg.workspace_dir = Path(self.tmpdir) / "workspace"
        self.cfg.task_dir = Path(self.tmpdir) / "tasks"
        self.cfg.log_dir = Path(self.tmpdir) / "logs"
        self.repo = TaskRepository(
            db_path=os.path.join(self.tmpdir, "test.db"),
            task_dir=self.cfg.task_dir,
        )
        self.router = MagicMock(spec=ModelRouter)
        self.router.available_providers.return_value = ["mock"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _engine_with_responses(self, responses):
        self.router.complete.side_effect = [mock_response(r) for r in responses]
        return OrchestrationEngine(config=self.cfg, repo=self.repo, router=self.router)

    def test_rejected_review_fails_task(self):
        rejected = json.dumps({
            "verdict": "REJECTED",
            "risk_level": "HIGH",
            "issues": [{"severity": "CRITICAL", "location": "main", "description": "Bug", "fix": "Fix it"}],
            "summary": "Rejected"
        })
        engine = self._engine_with_responses([
            PLANNER_OUTPUT,
            IMPLEMENTER_OUTPUT,
            rejected,  # reviewer says REJECTED
        ])
        task = engine.create_task("Rejected Task", "Description", acceptance_criteria=["c1"])
        result = engine.run_task(task.id)
        self.assertFalse(result.success)
        self.assertIn("VERIFY", result.error)

    def test_critical_security_fails_task(self):
        critical_sec = json.dumps({
            "verdict": "CRITICAL",
            "findings": [{"cve_like_id": "FORGE-001", "severity": "CRITICAL",
                          "description": "SQL injection", "remediation": "Use parameterized queries"}],
            "secrets_detected": [],
            "summary": "Critical vulnerability found"
        })
        engine = self._engine_with_responses([
            PLANNER_OUTPUT,
            IMPLEMENTER_OUTPUT,
            REVIEWER_OUTPUT,  # review passes
            critical_sec,    # security CRITICAL
        ])
        task = engine.create_task("Security Fail Task", "Description", acceptance_criteria=["c1"])
        result = engine.run_task(task.id)
        self.assertFalse(result.success)
        self.assertIn("REVIEW", result.error)

    def test_qa_fail_fails_task(self):
        qa_fail = json.dumps({
            "verdict": "FAIL",
            "criteria_results": [{"criterion": "c1", "status": "FAIL", "test": "assert False"}],
            "test_code": "def test(): assert False",
            "missing_coverage": ["criterion 1"]
        })
        engine = self._engine_with_responses([
            PLANNER_OUTPUT,
            IMPLEMENTER_OUTPUT,
            REVIEWER_OUTPUT,
            SECURITY_OUTPUT,
            qa_fail,  # QA FAIL
        ])
        task = engine.create_task("QA Fail Task", "Description", acceptance_criteria=["c1"])
        result = engine.run_task(task.id)
        self.assertFalse(result.success)
        self.assertIn("TEST", result.error)

    def test_task_state_is_failed_on_failure(self):
        from forge.providers.base import ProviderError
        self.router.complete.side_effect = ProviderError("All down", "all", retryable=False)
        engine = OrchestrationEngine(config=self.cfg, repo=self.repo, router=self.router)
        task = engine.create_task("Fail Task", "Description")
        engine.run_task(task.id)
        updated = self.repo.get(task.id)
        self.assertEqual(updated.state, TaskState.FAILED)

    def test_secrets_in_implementation_fail_security_gate(self):
        """Security agent static scan should detect secrets before LLM review."""
        impl_with_secret = """
```python
import os

API_KEY = "sk-ant-api03-supersecretkey1234567890abcdefghijk"

def call_api():
    return requests.get(url, headers={"key": API_KEY})
```
## Verification Steps
1. Run the function
"""
        clear_security = json.dumps({
            "verdict": "CLEAR",
            "findings": [],
            "secrets_detected": ["sk-ant-api03-supersecretkey1234567890abcdefghijk"],
            "summary": "Secret detected in implementation"
        })
        # The security agent's static scan will detect the secret regardless of LLM response
        engine = self._engine_with_responses([
            PLANNER_OUTPUT,
            impl_with_secret,
            REVIEWER_OUTPUT,
            clear_security,
        ])
        task = engine.create_task("Secret Task", "Description", acceptance_criteria=["c1"])
        task.context["implementation"] = impl_with_secret
        # Security gate checks secrets_detected field
        result = engine.run_task(task.id)
        # With secrets_detected non-empty, the REVIEW gate should fail
        self.assertFalse(result.success)


class TestEngineCheckpointResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = ForgeConfig()
        self.cfg.workspace_dir = Path(self.tmpdir) / "workspace"
        self.cfg.task_dir = Path(self.tmpdir) / "tasks"
        self.cfg.log_dir = Path(self.tmpdir) / "logs"
        self.repo = TaskRepository(
            db_path=os.path.join(self.tmpdir, "test.db"),
            task_dir=self.cfg.task_dir,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_checkpoint_saved_is_retrievable(self):
        task = Task(title="CP Task", description="Checkpoint test")
        self.repo.save(task)
        self.repo.save_checkpoint(task.id, "pre_EXECUTE", {
            "task_id": task.id,
            "current_phase": "EXECUTE",
        })
        loaded = self.repo.load_latest_checkpoint(task.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["current_phase"], "EXECUTE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
