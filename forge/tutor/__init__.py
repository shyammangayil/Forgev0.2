"""
FORGE Distilled Self-Tutor
==========================
Evolutionary learning system that extracts reusable engineering
principles from execution traces, repositories, and specifications.

Both a standalone system and a Forge component.

Public API::

    from forge.tutor import SelfTutor, get_tutor

    tutor = get_tutor()
    tutor.learn_from_task(task, result)
    tutor.learn_from_repo("/path/to/repo")
    tutor.learn_from_spec("requirements.md")
    tutor.learn_from_trace(raw_trace_text)
    enriched = tutor.enrich_prompt(system_prompt, agent_name="planner")
    print(tutor.report())
"""
from forge.tutor.tutor import SelfTutor
from forge.tutor.evidence import (
    Evidence,
    parse_trace,
    evidence_from_trace,
    evidence_from_spec,
    evidence_from_repo_report,
    evidence_from_task_result,
)
from forge.tutor.principles import (
    Principle,
    PrincipleEngine,
    MIN_CONFIDENCE,
    HIGH_CONFIDENCE,
)
from forge.tutor.store import PrincipleStore, get_principle_store

import threading

_tutor: "SelfTutor | None" = None
_tutor_lock = threading.Lock()


def get_tutor(db_path: str = "forge_tasks.db") -> SelfTutor:
    """Thread-safe module-level singleton."""
    global _tutor
    if _tutor is None:
        with _tutor_lock:
            if _tutor is None:
                _tutor = SelfTutor(db_path=db_path)
    return _tutor


__all__ = [
    "SelfTutor",
    "get_tutor",
    "Evidence",
    "Principle",
    "PrincipleEngine",
    "PrincipleStore",
    "get_principle_store",
    "parse_trace",
    "evidence_from_trace",
    "evidence_from_spec",
    "evidence_from_repo_report",
    "evidence_from_task_result",
    "MIN_CONFIDENCE",
    "HIGH_CONFIDENCE",
]
