"""
FORGE Tutor — Principle Store
==============================
Persists Principle objects to the existing memory store under
memory_type='principle', keyed by principle_id.

The store is the only component that writes to persistent state.
All other tutor components are stateless.

Operations:
  upsert(principle, action)  — add, confirm, or contradict
  get(principle_id)          — retrieve one
  list_active(category)      — all active principles, optionally filtered
  list_core()                — high-confidence principles for prompt injection
  stats()                    — summary dict
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from forge.memory.store import get_memory_store
from forge.telemetry.logger import get_logger
from forge.tutor.principles import (
    Principle,
    MIN_CONFIDENCE,
    HIGH_CONFIDENCE,
    _make_id,
)

log = get_logger("tutor.store")


class PrincipleStore:
    """
    Thin wrapper around MemoryStore that manages Principle lifecycle.

    All principles are stored as JSON in the 'value' field of a
    MemoryEntry with memory_type='principle'.

    Key format: principle:<principle_id>
    """

    MEMORY_TYPE = "principle"
    KEY_PREFIX  = "principle:"

    def __init__(self, db_path: str = "forge_tasks.db"):
        self._db_path = db_path

    def _store(self):
        return get_memory_store(self._db_path)

    def upsert(self, principle: Principle, action: str = "add") -> Principle:
        """
        Add a new principle or update an existing one.

        action: 'add'       — store if new, confirm if existing
                'confirm'   — increase confidence
                'contradict'— decrease confidence
        """
        key = self.KEY_PREFIX + principle.principle_id

        # Load existing if present
        try:
            existing_entry = self._load_entry(key)
        except Exception:
            existing_entry = None

        if existing_entry:
            existing = Principle.from_dict(existing_entry)
            if action == "contradict":
                existing.contradict()
                log.info("Principle contradicted",
                         id=principle.principle_id,
                         confidence=existing.confidence,
                         active=existing.active)
            else:
                # 'add' of existing == confirm (independent source)
                existing.confirm()
                log.info("Principle confirmed",
                         id=principle.principle_id,
                         confidence=existing.confidence,
                         core=existing.core)
            principle = existing
        else:
            log.info("New principle stored",
                     id=principle.principle_id,
                     category=principle.category,
                     confidence=principle.confidence)

        # Write back
        self._store().remember(
            memory_type = self.MEMORY_TYPE,
            key         = key,
            value       = json.dumps(principle.to_dict()),
            tags        = ["tutor", principle.category] + principle.tags[:3],
            confidence  = principle.confidence,
        )
        return principle

    def get(self, principle_id: str) -> Optional[Principle]:
        """Retrieve one principle by id."""
        try:
            entry = self._load_entry(self.KEY_PREFIX + principle_id)
            return Principle.from_dict(entry) if entry else None
        except Exception:
            return None

    def list_active(
        self,
        category: Optional[str] = None,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> List[Principle]:
        """All active principles, optionally filtered by category."""
        try:
            entries = self._store().recall_all(
                memory_type = self.MEMORY_TYPE,
                max_results = 200,
            )
        except Exception:
            return []

        principles = []
        for e in entries:
            if not e.key.startswith(self.KEY_PREFIX):
                continue
            if e.confidence < min_confidence:
                continue
            try:
                p = Principle.from_dict(json.loads(e.value))
                if not p.active:
                    continue
                if category and p.category != category:
                    continue
                principles.append(p)
            except Exception:
                continue

        return sorted(principles, key=lambda p: p.confidence, reverse=True)

    def list_core(self) -> List[Principle]:
        """Principles with confidence >= HIGH_CONFIDENCE — for prompt injection."""
        return [p for p in self.list_active() if p.core]

    def list_by_tags(self, *tags: str) -> List[Principle]:
        """Principles matching any of the given tags."""
        tag_set = set(tags)
        return [
            p for p in self.list_active()
            if set(p.tags) & tag_set
        ]

    def to_prompt_block(
        self,
        categories: Optional[List[str]] = None,
        max_principles: int = 8,
    ) -> str:
        """
        Format active principles as a block for agent prompt injection.

        Sorted by confidence descending. Core principles always included.
        Used by _inject_memories to enrich agent system prompts.
        """
        if categories:
            all_p = []
            for cat in categories:
                all_p.extend(self.list_active(category=cat))
            # Deduplicate
            seen = set()
            principles = []
            for p in sorted(all_p, key=lambda x: x.confidence, reverse=True):
                if p.principle_id not in seen:
                    principles.append(p)
                    seen.add(p.principle_id)
        else:
            principles = self.list_active()

        if not principles:
            return ""

        # Core first, then by confidence
        core    = [p for p in principles if p.core][:3]
        others  = [p for p in principles if not p.core]
        ordered = core + others
        ordered = ordered[:max_principles]

        lines = ["=== ENGINEERING PRINCIPLES (learned from successful patterns) ==="]
        for p in ordered:
            marker = "★" if p.core else " "
            lines.append(
                f"{marker} [{p.category}] {p.statement}"
                f"  (confidence={p.confidence:.0%}, sources={p.source_count})"
            )
        lines.append("=== END PRINCIPLES ===")
        return "\n".join(lines)

    def stats(self) -> dict:
        """Summary statistics for CLI output."""
        all_p = self.list_active(min_confidence=0.0)
        active = [p for p in all_p if p.active]
        core   = [p for p in active if p.core]
        by_cat: Dict[str, int] = {}
        for p in active:
            by_cat[p.category] = by_cat.get(p.category, 0) + 1
        return {
            "total":      len(all_p),
            "active":     len(active),
            "core":       len(core),
            "inactive":   len(all_p) - len(active),
            "by_category": by_cat,
            "mean_confidence": round(
                sum(p.confidence for p in active) / len(active), 3
            ) if active else 0.0,
        }

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_entry(self, key: str) -> Optional[dict]:
        """Load one entry's value as a dict, or None if not found."""
        entries = self._store().recall_all(
            memory_type = self.MEMORY_TYPE,
            max_results = 500,
        )
        for e in entries:
            if e.key == key:
                return json.loads(e.value)
        return None


# ── Module-level singleton ─────────────────────────────────────────────────────

_store: Optional[PrincipleStore] = None


def get_principle_store(db_path: str = "forge_tasks.db") -> PrincipleStore:
    global _store
    if _store is None:
        _store = PrincipleStore(db_path)
    return _store
