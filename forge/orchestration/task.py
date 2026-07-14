"""
FORGE Task Model
================
Every unit of work is a Task. Tasks persist as TASK-NNN.md files and in the
database. Parent tasks can only complete when all children are complete.

State machine:
  NEW → READY → IN_PROGRESS → VERIFIED → COMPLETE
                            → FAILED
                            → BLOCKED
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class TaskState(str, Enum):
    NEW = "NEW"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"
    COMPLETE = "COMPLETE"


class TaskType(str, Enum):
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"
    SECURITY = "security"
    DOCUMENTATION = "documentation"
    PLANNING = "planning"
    LARGE_CONTEXT = "large_context"
    ANALYSIS  = "analysis"
    DEBUGGING = "debugging"   # investigation-first codebase fix


TERMINAL_STATES = {TaskState.COMPLETE, TaskState.FAILED}
VALID_TRANSITIONS: Dict[TaskState, set] = {
    TaskState.NEW: {TaskState.READY, TaskState.FAILED},
    TaskState.READY: {TaskState.IN_PROGRESS, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.IN_PROGRESS: {TaskState.VERIFIED, TaskState.FAILED, TaskState.BLOCKED},
    TaskState.BLOCKED: {TaskState.READY, TaskState.FAILED},
    TaskState.FAILED: {TaskState.READY},  # allow retry
    TaskState.VERIFIED: {TaskState.COMPLETE, TaskState.FAILED},
    TaskState.COMPLETE: set(),  # terminal
}


@dataclass
class TaskResult:
    success: bool
    output: Any
    error: Optional[str] = None
    tokens_used: int = 0
    provider_used: str = ""
    duration_ms: int = 0
    validation_passed: bool = False
    review_passed: bool = False
    tests_passed: bool = False


@dataclass
class Task:
    title: str
    description: str
    task_type: TaskType = TaskType.CODING
    acceptance_criteria: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8].upper())
    parent_id: Optional[str] = None
    child_ids: List[str] = field(default_factory=list)
    state: TaskState = TaskState.NEW
    priority: int = 5  # 1=highest, 10=lowest
    tags: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    result: Optional[TaskResult] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    assigned_agent: Optional[str] = None
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)
    audit_log: List[Dict] = field(default_factory=list)
    sequence_number: Optional[int] = None  # TASK-NNN number

    def transition(self, new_state: TaskState, reason: str = "") -> bool:
        """Attempt a state transition. Returns True on success."""
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            return False
        old_state = self.state
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)

        if new_state == TaskState.IN_PROGRESS and not self.started_at:
            self.started_at = self.updated_at
        if new_state in TERMINAL_STATES:
            self.completed_at = self.updated_at

        self.audit_log.append({
            "timestamp": self.updated_at.isoformat(),
            "from_state": old_state.value,
            "to_state": new_state.value,
            "reason": reason,
        })
        return True

    def can_complete(self) -> bool:
        """A task can only complete in VERIFIED state with all criteria met."""
        return self.state == TaskState.VERIFIED

    def is_blocked_by_children(self, all_tasks: Dict[str, "Task"]) -> bool:
        return any(
            all_tasks[cid].state not in TERMINAL_STATES
            for cid in self.child_ids
            if cid in all_tasks
        )

    def to_markdown(self) -> str:
        """Render task as TASK-NNN.md content."""
        seq = f"TASK-{self.sequence_number:03d}" if self.sequence_number else f"TASK-{self.id}"
        parent_ref = f"TASK-{self.parent_id}" if self.parent_id else "None"
        children_ref = ", ".join(f"TASK-{c}" for c in self.child_ids) or "None"
        criteria_md = "\n".join(f"- [ ] {c}" for c in self.acceptance_criteria) or "- [ ] (none specified)"

        audit_md = ""
        for entry in self.audit_log[-10:]:  # last 10 transitions
            audit_md += f"| {entry['timestamp']} | {entry['from_state']} → {entry['to_state']} | {entry.get('reason', '')} |\n"

        result_md = ""
        if self.result:
            r = self.result
            result_md = f"""
## Result
- **Success**: {r.success}
- **Provider**: {r.provider_used}
- **Tokens Used**: {r.tokens_used:,}
- **Duration**: {r.duration_ms}ms
- **Validation Passed**: {r.validation_passed}
- **Review Passed**: {r.review_passed}
- **Tests Passed**: {r.tests_passed}
{f'- **Error**: {r.error}' if r.error else ''}
"""

        return f"""# {seq}: {self.title}

## Metadata
| Field | Value |
|-------|-------|
| ID | `{self.id}` |
| State | **{self.state.value}** |
| Type | {self.task_type.value} |
| Priority | {self.priority} |
| Parent | {parent_ref} |
| Children | {children_ref} |
| Assigned Agent | {self.assigned_agent or 'Unassigned'} |
| Created | {self.created_at.isoformat()} |
| Updated | {self.updated_at.isoformat()} |
| Started | {self.started_at.isoformat() if self.started_at else 'Not started'} |
| Completed | {self.completed_at.isoformat() if self.completed_at else 'Not completed'} |

## Description
{self.description}

## Acceptance Criteria
{criteria_md}
{result_md}
## Audit Log
| Timestamp | Transition | Reason |
|-----------|-----------|--------|
{audit_md or '| — | — | — |\n'}

## Context
```json
{json.dumps(self.context, indent=2, default=str)}
```
"""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sequence_number": self.sequence_number,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type.value,
            "state": self.state.value,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "child_ids": self.child_ids,
            "acceptance_criteria": self.acceptance_criteria,
            "tags": self.tags,
            "context": self.context,
            "assigned_agent": self.assigned_agent,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "audit_log": self.audit_log,
            "checkpoint_data": self.checkpoint_data,
            "result": {
                "success": self.result.success,
                "output": str(self.result.output)[:500] if self.result.output else None,
                "error": self.result.error,
                "tokens_used": self.result.tokens_used,
                "provider_used": self.result.provider_used,
                "duration_ms": self.result.duration_ms,
                "validation_passed": self.result.validation_passed,
                "review_passed": self.result.review_passed,
                "tests_passed": self.result.tests_passed,
            } if self.result else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        t = cls(
            title=data["title"],
            description=data["description"],
            task_type=TaskType(data.get("task_type", "coding")),
        )
        t.id = data["id"]
        t.sequence_number = data.get("sequence_number")
        t.state = TaskState(data.get("state", "NEW"))
        t.priority = data.get("priority", 5)
        t.parent_id = data.get("parent_id")
        t.child_ids = data.get("child_ids", [])
        t.acceptance_criteria = data.get("acceptance_criteria", [])
        t.tags = data.get("tags", [])
        t.context = data.get("context", {})
        t.assigned_agent = data.get("assigned_agent")
        t.audit_log = data.get("audit_log", [])
        t.checkpoint_data = data.get("checkpoint_data", {})

        def _parse_dt(v):
            return datetime.fromisoformat(v) if v else None

        t.created_at = _parse_dt(data.get("created_at")) or datetime.now(timezone.utc)
        t.updated_at = _parse_dt(data.get("updated_at")) or t.created_at
        t.started_at = _parse_dt(data.get("started_at"))
        t.completed_at = _parse_dt(data.get("completed_at"))

        if data.get("result"):
            r = data["result"]
            t.result = TaskResult(
                success=r["success"],
                output=r.get("output"),
                error=r.get("error"),
                tokens_used=r.get("tokens_used", 0),
                provider_used=r.get("provider_used", ""),
                duration_ms=r.get("duration_ms", 0),
                validation_passed=r.get("validation_passed", False),
                review_passed=r.get("review_passed", False),
                tests_passed=r.get("tests_passed", False),
            )
        return t
