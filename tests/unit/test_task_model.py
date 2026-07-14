"""
Unit Tests: Task Model
======================
Tests the Task dataclass, state machine, markdown rendering, and serialisation.
No LLM calls. No network. Pure logic.
"""
import json
import sys
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Make forge importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.orchestration.task import (
    Task, TaskState, TaskType, TaskResult, VALID_TRANSITIONS
)


class TestTaskCreation(unittest.TestCase):
    def test_default_state_is_new(self):
        task = Task(title="Test", description="Desc")
        self.assertEqual(task.state, TaskState.NEW)

    def test_default_type_is_coding(self):
        task = Task(title="Test", description="Desc")
        self.assertEqual(task.task_type, TaskType.CODING)

    def test_id_auto_generated(self):
        t1 = Task(title="A", description="D")
        t2 = Task(title="B", description="D")
        self.assertNotEqual(t1.id, t2.id)
        self.assertEqual(len(t1.id), 8)

    def test_timestamps_set_on_creation(self):
        task = Task(title="T", description="D")
        self.assertIsNotNone(task.created_at)
        self.assertIsNotNone(task.updated_at)

    def test_empty_audit_log_on_creation(self):
        task = Task(title="T", description="D")
        self.assertEqual(task.audit_log, [])

    def test_empty_child_ids_on_creation(self):
        task = Task(title="T", description="D")
        self.assertEqual(task.child_ids, [])


class TestTaskStateTransitions(unittest.TestCase):
    def _task(self):
        return Task(title="Test", description="Desc")

    def test_new_to_ready(self):
        task = self._task()
        result = task.transition(TaskState.READY, "test")
        self.assertTrue(result)
        self.assertEqual(task.state, TaskState.READY)

    def test_new_to_failed(self):
        task = self._task()
        self.assertTrue(task.transition(TaskState.FAILED, "test"))
        self.assertEqual(task.state, TaskState.FAILED)

    def test_full_happy_path(self):
        task = self._task()
        transitions = [
            TaskState.READY,
            TaskState.IN_PROGRESS,
            TaskState.VERIFIED,
            TaskState.COMPLETE,
        ]
        for state in transitions:
            ok = task.transition(state, "test")
            self.assertTrue(ok, f"Failed to transition to {state}")
        self.assertEqual(task.state, TaskState.COMPLETE)

    def test_invalid_transition_rejected(self):
        task = self._task()  # state = NEW
        result = task.transition(TaskState.COMPLETE, "skip ahead")
        self.assertFalse(result)
        self.assertEqual(task.state, TaskState.NEW)

    def test_complete_is_terminal(self):
        task = self._task()
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        task.transition(TaskState.VERIFIED)
        task.transition(TaskState.COMPLETE)
        # Cannot escape COMPLETE
        self.assertFalse(task.transition(TaskState.NEW))
        self.assertFalse(task.transition(TaskState.READY))
        self.assertEqual(task.state, TaskState.COMPLETE)

    def test_failed_can_retry_to_ready(self):
        task = self._task()
        task.transition(TaskState.FAILED)
        self.assertTrue(task.transition(TaskState.READY, "retry"))
        self.assertEqual(task.state, TaskState.READY)

    def test_transition_updates_audit_log(self):
        task = self._task()
        task.transition(TaskState.READY, "initiating")
        self.assertEqual(len(task.audit_log), 1)
        entry = task.audit_log[0]
        self.assertEqual(entry["from_state"], "NEW")
        self.assertEqual(entry["to_state"], "READY")
        self.assertEqual(entry["reason"], "initiating")

    def test_in_progress_sets_started_at(self):
        task = self._task()
        task.transition(TaskState.READY)
        self.assertIsNone(task.started_at)
        task.transition(TaskState.IN_PROGRESS)
        self.assertIsNotNone(task.started_at)

    def test_complete_sets_completed_at(self):
        task = self._task()
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        task.transition(TaskState.VERIFIED)
        task.transition(TaskState.COMPLETE)
        self.assertIsNotNone(task.completed_at)

    def test_blocked_from_in_progress(self):
        task = self._task()
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        self.assertTrue(task.transition(TaskState.BLOCKED, "waiting on deps"))

    def test_unblocked_to_ready(self):
        task = self._task()
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        task.transition(TaskState.BLOCKED)
        self.assertTrue(task.transition(TaskState.READY, "unblocked"))


class TestTaskCanComplete(unittest.TestCase):
    def test_cannot_complete_from_new(self):
        task = Task(title="T", description="D")
        self.assertFalse(task.can_complete())

    def test_cannot_complete_from_in_progress(self):
        task = Task(title="T", description="D")
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        self.assertFalse(task.can_complete())

    def test_can_complete_from_verified(self):
        task = Task(title="T", description="D")
        task.transition(TaskState.READY)
        task.transition(TaskState.IN_PROGRESS)
        task.transition(TaskState.VERIFIED)
        self.assertTrue(task.can_complete())


class TestTaskMarkdown(unittest.TestCase):
    def test_markdown_contains_title(self):
        task = Task(title="My Task", description="Some desc")
        task.sequence_number = 42
        md = task.to_markdown()
        self.assertIn("My Task", md)
        self.assertIn("TASK-042", md)

    def test_markdown_contains_state(self):
        task = Task(title="T", description="D")
        md = task.to_markdown()
        self.assertIn("NEW", md)

    def test_markdown_contains_criteria(self):
        task = Task(title="T", description="D",
                    acceptance_criteria=["Must work", "Must be fast"])
        md = task.to_markdown()
        self.assertIn("Must work", md)
        self.assertIn("Must be fast", md)

    def test_markdown_contains_result_when_set(self):
        task = Task(title="T", description="D")
        task.result = TaskResult(
            success=True, output="done",
            provider_used="anthropic", tokens_used=1234
        )
        md = task.to_markdown()
        self.assertIn("anthropic", md)
        self.assertIn("1,234", md)

    def test_markdown_contains_audit_log(self):
        task = Task(title="T", description="D")
        task.transition(TaskState.READY, "test transition")
        md = task.to_markdown()
        self.assertIn("NEW", md)
        self.assertIn("READY", md)


class TestTaskSerialisation(unittest.TestCase):
    def _full_task(self):
        task = Task(
            title="Full Task",
            description="Full description",
            task_type=TaskType.REVIEW,
            acceptance_criteria=["crit1", "crit2"],
            priority=3,
            tags=["backend", "api"],
            context={"key": "value"},
        )
        task.sequence_number = 7
        task.transition(TaskState.READY, "start")
        task.transition(TaskState.IN_PROGRESS, "running")
        task.result = TaskResult(
            success=True, output="output",
            provider_used="openai", tokens_used=500,
            validation_passed=True, review_passed=True, tests_passed=True,
        )
        return task

    def test_to_dict_contains_all_fields(self):
        task = self._full_task()
        d = task.to_dict()
        for field in ["id", "title", "description", "task_type", "state",
                      "priority", "acceptance_criteria", "audit_log", "result"]:
            self.assertIn(field, d)

    def test_round_trip_preserves_state(self):
        task = self._full_task()
        d = task.to_dict()
        restored = Task.from_dict(d)
        self.assertEqual(restored.state, TaskState.IN_PROGRESS)
        self.assertEqual(restored.task_type, TaskType.REVIEW)
        self.assertEqual(restored.priority, 3)
        self.assertEqual(restored.acceptance_criteria, ["crit1", "crit2"])
        self.assertEqual(restored.sequence_number, 7)

    def test_round_trip_preserves_result(self):
        task = self._full_task()
        restored = Task.from_dict(task.to_dict())
        self.assertIsNotNone(restored.result)
        self.assertTrue(restored.result.success)
        self.assertEqual(restored.result.provider_used, "openai")
        self.assertEqual(restored.result.tokens_used, 500)

    def test_round_trip_preserves_audit_log(self):
        task = self._full_task()
        restored = Task.from_dict(task.to_dict())
        self.assertEqual(len(restored.audit_log), 2)

    def test_from_dict_handles_missing_optional_fields(self):
        minimal = {"title": "T", "description": "D", "id": "abc12345"}
        task = Task.from_dict(minimal)
        self.assertEqual(task.title, "T")
        self.assertEqual(task.state, TaskState.NEW)
        self.assertIsNone(task.result)


class TestChildTaskBlocking(unittest.TestCase):
    def test_parent_blocked_by_incomplete_child(self):
        parent = Task(title="Parent", description="Parent task")
        child = Task(title="Child", description="Child task")
        parent.child_ids = [child.id]

        all_tasks = {parent.id: parent, child.id: child}
        # Child is still NEW — parent should report blocked
        self.assertTrue(parent.is_blocked_by_children(all_tasks))

    def test_parent_not_blocked_when_children_complete(self):
        parent = Task(title="Parent", description="Parent task")
        child = Task(title="Child", description="Child task")
        parent.child_ids = [child.id]

        child.transition(TaskState.READY)
        child.transition(TaskState.IN_PROGRESS)
        child.transition(TaskState.VERIFIED)
        child.transition(TaskState.COMPLETE)

        all_tasks = {parent.id: parent, child.id: child}
        self.assertFalse(parent.is_blocked_by_children(all_tasks))

    def test_parent_not_blocked_with_no_children(self):
        parent = Task(title="Parent", description="Parent task")
        self.assertFalse(parent.is_blocked_by_children({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
