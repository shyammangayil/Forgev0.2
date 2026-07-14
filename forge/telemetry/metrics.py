"""
FORGE Metrics
=============
Lightweight metrics collector. Outputs Prometheus text format.
Replace with prometheus_client when available.
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Counter:
    name: str
    help: str
    labels: Dict[str, str] = field(default_factory=dict)
    _value: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, amount: float = 1.0):
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        return self._value


@dataclass
class Histogram:
    name: str
    help: str
    buckets: List[float] = field(default_factory=lambda: [.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10])
    _observations: List[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float):
        with self._lock:
            self._observations.append(value)

    @property
    def count(self) -> int:
        return len(self._observations)

    @property
    def sum(self) -> float:
        return sum(self._observations)

    @property
    def p50(self) -> float:
        if not self._observations:
            return 0
        s = sorted(self._observations)
        return s[len(s) // 2]

    @property
    def p95(self) -> float:
        if not self._observations:
            return 0
        s = sorted(self._observations)
        return s[int(len(s) * 0.95)]

    @property
    def p99(self) -> float:
        if not self._observations:
            return 0
        s = sorted(self._observations)
        return s[int(len(s) * 0.99)]


class MetricsRegistry:
    """Central registry for all FORGE metrics."""

    def __init__(self):
        self._counters: Dict[str, Counter] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._lock = threading.Lock()
        self._start_time = time.time()

        # Pre-register standard metrics
        self._init_metrics()

    def _init_metrics(self):
        metrics = [
            ("forge_llm_requests_total", "Total LLM API requests"),
            ("forge_llm_failures_total", "Total LLM API failures"),
            ("forge_llm_retries_total", "Total LLM retry attempts"),
            ("forge_llm_failovers_total", "Total provider failovers"),
            ("forge_tasks_created_total", "Tasks created"),
            ("forge_tasks_completed_total", "Tasks completed"),
            ("forge_tasks_failed_total", "Tasks failed"),
            ("forge_tasks_blocked_total", "Tasks transitioned to BLOCKED state"),
            ("forge_supervisor_runs_total", "Supervisor loop invocations"),
            ("forge_learning_runs_total", "Post-task LearningEngine runs"),
            ("forge_tutor_runs_total",    "Post-task SelfTutor principle extraction runs"),
            ("forge_privacy_findings_total", "Privacy gateway findings detected"),
            ("forge_privacy_redactions_total", "Privacy gateway redactions applied"),
            ("forge_privacy_blocks_total", "Executions blocked by privacy gate"),
            ("forge_agent_invocations_total", "Agent invocations"),
            ("forge_tool_calls_total", "Tool invocations by agents"),
            ("forge_cache_hits_total", "Inference cache hits"),
            ("forge_hallucination_detections_total", "Hallucination detections"),
            ("forge_tokens_used_total", "Total tokens consumed"),
            ("forge_cost_usd_total", "Estimated cost in USD"),
        ]
        for name, help_text in metrics:
            self._counters[name] = Counter(name=name, help=help_text)

        histograms = [
            ("forge_llm_latency_seconds", "LLM request latency"),
            ("forge_task_duration_seconds", "Task completion duration"),
            ("forge_agent_duration_seconds", "Agent execution duration"),
        ]
        for name, help_text in histograms:
            self._histograms[name] = Histogram(name=name, help=help_text)

    def counter(self, name: str) -> Counter:
        return self._counters.get(name, Counter(name=name, help=""))

    def histogram(self, name: str) -> Histogram:
        return self._histograms.get(name, Histogram(name=name, help=""))

    def inc(self, name: str, amount: float = 1.0):
        if name in self._counters:
            self._counters[name].inc(amount)

    def observe(self, name: str, value: float):
        if name in self._histograms:
            self._histograms[name].observe(value)

    def snapshot(self) -> dict:
        uptime = time.time() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "counters": {k: v.value for k, v in self._counters.items()},
            "histograms": {
                k: {
                    "count": v.count,
                    "sum": round(v.sum, 4),
                    "p50": round(v.p50, 4),
                    "p95": round(v.p95, 4),
                    "p99": round(v.p99, 4),
                }
                for k, v in self._histograms.items()
            },
        }

    def prometheus_text(self) -> str:
        lines = []
        for name, c in self._counters.items():
            lines.append(f"# HELP {name} {c.help}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {c.value}")
        for name, h in self._histograms.items():
            lines.append(f"# HELP {name} {h.help}")
            lines.append(f"# TYPE {name} histogram")
            lines.append(f"{name}_count {h.count}")
            lines.append(f"{name}_sum {h.sum:.4f}")
        return "\n".join(lines)


# Global registry
_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _registry