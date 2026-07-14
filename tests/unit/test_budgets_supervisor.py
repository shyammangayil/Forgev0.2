"""
Unit tests for RetryBudget, ConsensusScore, and Supervisor wiring.
Pure stdlib unittest — no pytest dependency.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.orchestration.budgets import RetryBudget, MAX_IMPL_RETRIES, MAX_REVIEW_RETRIES
from forge.orchestration.supervisor import ConsensusScore, create_repair_context


def _approx_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ── RetryBudget ────────────────────────────────────────────────────────────────

class TestRetryBudget(unittest.TestCase):

    def test_fresh_budget_has_full_allowance(self):
        b = RetryBudget(max_impl_retries=5, max_review_retries=3, max_test_retries=5)
        self.assertTrue(b.can_retry("implement"))
        self.assertTrue(b.can_retry("review"))
        self.assertTrue(b.can_retry("test"))

    def test_budget_exhausted_after_max_retries(self):
        b = RetryBudget(max_impl_retries=2)
        b.consume("implement")
        b.consume("implement")
        self.assertFalse(b.can_retry("implement"))

    def test_independent_phase_budgets(self):
        b = RetryBudget(max_impl_retries=1, max_review_retries=3)
        b.consume("implement")
        self.assertFalse(b.can_retry("implement"))
        self.assertTrue(b.can_retry("review"))

    def test_global_iteration_ceiling(self):
        b = RetryBudget(max_loop_iterations=3)
        b.consume("implement")
        b.consume("implement")
        b.consume("implement")
        self.assertFalse(b.can_retry("implement"))
        self.assertIn("ceiling", b.exhausted_reason("implement").lower())

    def test_roundtrip_serialisation(self):
        b = RetryBudget(max_impl_retries=4, max_review_retries=2)
        b.consume("implement")
        b.consume("review")
        b2 = RetryBudget.from_dict(b.to_dict())
        self.assertEqual(b2.remaining("implement"), b.remaining("implement"))
        self.assertEqual(b2.remaining("review"), b.remaining("review"))
        self.assertEqual(b2._total_iterations, b._total_iterations)

    def test_remaining_decrements(self):
        b = RetryBudget(max_impl_retries=5)
        self.assertEqual(b.remaining("implement"), 5)
        b.consume("implement")
        self.assertEqual(b.remaining("implement"), 4)


# ── ConsensusScore ─────────────────────────────────────────────────────────────

class TestConsensusScore(unittest.TestCase):

    def test_all_pass_gives_score_1(self):
        cs = ConsensusScore.compute(
            review   = {"verdict": "APPROVED"},
            security = {"verdict": "CLEAR"},
            qa       = {"verdict": "PASS"},
        )
        self.assertTrue(_approx_equal(cs.weighted_score, 1.0))
        self.assertTrue(cs.passes)

    def test_all_fail_gives_score_0(self):
        cs = ConsensusScore.compute(
            review   = {"verdict": "REJECTED"},
            security = {"verdict": "CRITICAL"},
            qa       = {"verdict": "FAIL"},
        )
        self.assertTrue(_approx_equal(cs.weighted_score, 0.0))
        self.assertFalse(cs.passes)

    def test_mixed_score_at_threshold(self):
        # reviewer APPROVED (1.0×0.4), security CRITICAL (0.0×0.35), qa PASS (1.0×0.25)
        # = 0.4 + 0.0 + 0.25 = 0.65  (exactly at default 0.65 threshold)
        cs = ConsensusScore.compute(
            review   = {"verdict": "APPROVED"},
            security = {"verdict": "CRITICAL"},
            qa       = {"verdict": "PASS"},
        )
        self.assertTrue(_approx_equal(cs.weighted_score, 0.65))

    def test_needs_changes_gives_partial_credit(self):
        cs = ConsensusScore.compute(
            review   = {"verdict": "NEEDS_CHANGES"},
            security = {"verdict": "WARNINGS"},
            qa       = {"verdict": "PARTIAL"},
        )
        # 0.5×0.4 + 0.5×0.35 + 0.5×0.25 = 0.5
        self.assertTrue(_approx_equal(cs.weighted_score, 0.5))

    def test_missing_data_gets_partial_credit(self):
        cs = ConsensusScore.compute(review=None, security=None, qa=None)
        # 0.3×0.4 + 0.3×0.35 + 0.3×0.25 = 0.3
        self.assertTrue(_approx_equal(cs.weighted_score, 0.3))
        self.assertFalse(cs.passes)

    def test_summary_contains_pass_or_fail(self):
        cs = ConsensusScore.compute(
            review   = {"verdict": "APPROVED"},
            security = {"verdict": "CLEAR"},
            qa       = {"verdict": "PASS"},
        )
        self.assertIn("PASS", cs.summary())


# ── create_repair_context ──────────────────────────────────────────────────────

class TestCreateRepairContext(unittest.TestCase):

    def _make_task(self):
        from forge.orchestration.task import Task, TaskType
        return Task(title="Test", description="desc", task_type=TaskType.CODING)

    def test_repair_context_has_directive(self):
        task = self._make_task()
        budget = RetryBudget()
        ctx = create_repair_context(task, "VERIFY", "gate failed", None, budget, attempt=1)
        self.assertIn("directive", ctx)
        self.assertIn("Repair #1", ctx["directive"])

    def test_repair_context_includes_consensus(self):
        task = self._make_task()
        budget = RetryBudget()
        cs = ConsensusScore.compute(
            {"verdict": "REJECTED"}, {"verdict": "CLEAR"}, {"verdict": "PASS"}
        )
        ctx = create_repair_context(task, "VERIFY", "rejected", cs, budget, attempt=2)
        self.assertIn("consensus", ctx)
        self.assertIsNotNone(ctx["consensus"]["score"])

    def test_repair_context_has_budget_remaining(self):
        task = self._make_task()
        budget = RetryBudget(max_impl_retries=5)
        budget.consume("implement")
        ctx = create_repair_context(task, "EXECUTE", "placeholder", None, budget, attempt=1)
        self.assertEqual(ctx["budget_remaining"]["implement"], 4)


# ── Integration: engine routes through Supervisor ──────────────────────────────

class TestEngineSupervisorWiring(unittest.TestCase):
    """Smoke-test that run_task() instantiates Supervisor and delegates to it."""

    def test_run_task_uses_supervisor(self):
        from forge.orchestration.engine import OrchestrationEngine
        from forge.orchestration.task import Task, TaskType, TaskState, TaskResult

        mock_router = MagicMock()
        mock_repo   = MagicMock()

        task = Task(title="T", description="D", task_type=TaskType.CODING)
        task.state = TaskState.NEW

        mock_repo.get.return_value = task
        mock_repo.load_latest_checkpoint.return_value = None
        mock_repo.save.return_value = task
        mock_repo.write_audit.return_value = None
        mock_repo.save_checkpoint.return_value = None

        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.config  = MagicMock()
        engine.config.ensure_dirs = lambda: None
        engine.repo    = mock_repo
        engine.router  = mock_router
        engine.agents  = {}

        with patch("forge.orchestration.engine.new_correlation_id"), \
             patch("forge.orchestration.engine.set_task_id"), \
             patch("forge.orchestration.supervisor.Supervisor") as MockSupervisor:

            mock_instance = MockSupervisor.return_value
            mock_instance.run.return_value = TaskResult(success=True, output={})

            result = engine.run_task(task.id)

        MockSupervisor.assert_called_once()
        mock_instance.run.assert_called_once()
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main(verbosity=2)
