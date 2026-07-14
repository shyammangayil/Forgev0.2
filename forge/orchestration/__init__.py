"""
FORGE Orchestration package.

Public API:
  OrchestrationEngine  — the main task runner (use engine.run_task())
  Supervisor           — the self-healing conductor (used by engine internally)
  RetryBudget          — per-phase budget dataclass (checkpointable)
  ConsensusScore       — aggregate quality signal across verification agents
"""
from forge.orchestration.engine import OrchestrationEngine, ExecutionContext, ExecutionPhase
from forge.orchestration.task import Task, TaskResult, TaskState, TaskType
from forge.orchestration.budgets import RetryBudget, MAX_IMPL_RETRIES, MAX_REVIEW_RETRIES, MAX_TEST_RETRIES, MAX_LOOP_ITERATIONS
from forge.orchestration.supervisor import Supervisor, ConsensusScore

__all__ = [
    "OrchestrationEngine",
    "ExecutionContext",
    "ExecutionPhase",
    "Task",
    "TaskResult",
    "TaskState",
    "TaskType",
    "RetryBudget",
    "Supervisor",
    "ConsensusScore",
    "MAX_IMPL_RETRIES",
    "MAX_REVIEW_RETRIES",
    "MAX_TEST_RETRIES",
    "MAX_LOOP_ITERATIONS",
]
