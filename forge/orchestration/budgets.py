"""
FORGE Retry Budgets
===================
Per-phase retry limits that prevent an autonomous loop from oscillating
forever.  Every budget is independently tracked so exhausting the
implementation budget does not consume the review budget, and vice-versa.

Defaults (all overridable via env vars):

  FORGE_MAX_IMPL_RETRIES      = 5   (how many times implementer may be re-run)
  FORGE_MAX_REVIEW_RETRIES    = 3   (reviewer re-runs after VERIFY gate fail)
  FORGE_MAX_TEST_RETRIES      = 5   (QA re-runs after TEST gate fail)
  FORGE_MAX_PROVIDER_RETRIES  = 3   (per-provider LLM call retries — also used by router)
  FORGE_MAX_LOOP_ITERATIONS   = 20  (hard ceiling on total phase executions)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# ── Defaults ──────────────────────────────────────────────────────────────────

MAX_IMPL_RETRIES     = _env_int("FORGE_MAX_IMPL_RETRIES",     5)
MAX_REVIEW_RETRIES   = _env_int("FORGE_MAX_REVIEW_RETRIES",   3)
MAX_TEST_RETRIES     = _env_int("FORGE_MAX_TEST_RETRIES",     5)
MAX_PROVIDER_RETRIES = _env_int("FORGE_MAX_PROVIDER_RETRIES", 3)
MAX_LOOP_ITERATIONS  = _env_int("FORGE_MAX_LOOP_ITERATIONS",  20)


@dataclass
class RetryBudget:
    """
    Independent, per-phase retry counters for one task execution.

    Counters are stored here (not on ExecutionContext) so they survive
    phase rewinding and are checkpointed independently.

    Usage::

        budget = RetryBudget()
        if budget.can_retry("implement"):
            budget.consume("implement")
            # re-run implementer
        else:
            # fail the task — budget exhausted
    """

    max_impl_retries:     int = field(default_factory=lambda: MAX_IMPL_RETRIES)
    max_review_retries:   int = field(default_factory=lambda: MAX_REVIEW_RETRIES)
    max_test_retries:     int = field(default_factory=lambda: MAX_TEST_RETRIES)
    max_loop_iterations:  int = field(default_factory=lambda: MAX_LOOP_ITERATIONS)

    _counts: Dict[str, int] = field(default_factory=dict)
    _total_iterations: int = 0

    # ── Public API ────────────────────────────────────────────────────────

    def can_retry(self, phase_key: str) -> bool:
        """Return True if this phase still has budget remaining."""
        if self._total_iterations >= self.max_loop_iterations:
            return False
        limit = self._limit_for(phase_key)
        return self._counts.get(phase_key, 0) < limit

    def consume(self, phase_key: str) -> None:
        """Record one retry for this phase and increment the global iteration counter."""
        self._counts[phase_key] = self._counts.get(phase_key, 0) + 1
        self._total_iterations += 1

    def remaining(self, phase_key: str) -> int:
        """Return remaining retries for a phase (may be negative if over-consumed)."""
        return self._limit_for(phase_key) - self._counts.get(phase_key, 0)

    def exhausted_reason(self, phase_key: str) -> str:
        if self._total_iterations >= self.max_loop_iterations:
            return (
                f"Global iteration ceiling reached "
                f"({self._total_iterations}/{self.max_loop_iterations})"
            )
        used  = self._counts.get(phase_key, 0)
        limit = self._limit_for(phase_key)
        return (
            f"Phase '{phase_key}' retry budget exhausted "
            f"({used}/{limit} attempts used)"
        )

    def to_dict(self) -> dict:
        return {
            "counts":           dict(self._counts),
            "total_iterations": self._total_iterations,
            "limits": {
                "impl":      self.max_impl_retries,
                "review":    self.max_review_retries,
                "test":      self.max_test_retries,
                "loop":      self.max_loop_iterations,
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetryBudget":
        """Restore a budget from a checkpoint dict."""
        b = cls(
            max_impl_retries    = data.get("limits", {}).get("impl",   MAX_IMPL_RETRIES),
            max_review_retries  = data.get("limits", {}).get("review", MAX_REVIEW_RETRIES),
            max_test_retries    = data.get("limits", {}).get("test",   MAX_TEST_RETRIES),
            max_loop_iterations = data.get("limits", {}).get("loop",   MAX_LOOP_ITERATIONS),
        )
        b._counts            = dict(data.get("counts", {}))
        b._total_iterations  = data.get("total_iterations", 0)
        return b

    # ── Internal ──────────────────────────────────────────────────────────

    def _limit_for(self, phase_key: str) -> int:
        _MAP = {
            "implement": self.max_impl_retries,
            "review":    self.max_review_retries,
            "test":      self.max_test_retries,
        }
        return _MAP.get(phase_key, self.max_impl_retries)
