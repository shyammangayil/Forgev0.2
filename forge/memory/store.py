"""
FORGE Memory Store
==================
Cross-task persistent memory. Agents can remember facts, preferences,
project context, and findings across completely separate task runs.

Memory is stored in the same SQLite database as tasks (separate table).
No vector database or embeddings required — uses full-text search (FTS5)
for retrieval, which works offline with Ollama.

Memory types
------------
  fact        — verified piece of information (e.g. "project uses Python 3.11")
  preference  — user or project preference (e.g. "use black for formatting")
  finding     — research finding from ResearchAgent
  error       — known failure pattern to avoid
  context     — ongoing project context

Env vars
--------
  FORGE_MEMORY_MAX_AGE_DAYS   memories older than this are archived (default: 90)
  FORGE_MEMORY_MAX_RESULTS    max results per recall query (default: 10)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from forge.telemetry.logger import get_logger

log = get_logger("memory")

_MAX_AGE_DAYS  = int(__import__("os").environ.get("FORGE_MEMORY_MAX_AGE_DAYS", "90"))
_MAX_RESULTS   = int(__import__("os").environ.get("FORGE_MEMORY_MAX_RESULTS", "10"))

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    tags        TEXT    NOT NULL DEFAULT '[]',
    task_id     TEXT,
    confidence  REAL    NOT NULL DEFAULT 1.0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    accessed_at TEXT    NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_key ON memory_entries(key);
CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries(memory_type);
CREATE INDEX IF NOT EXISTS idx_memory_task ON memory_entries(task_id);
CREATE INDEX IF NOT EXISTS idx_memory_archived ON memory_entries(archived);

-- Full-text search over key + value
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    key, value,
    content='memory_entries',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_entries BEGIN
    INSERT INTO memory_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, value) VALUES ('delete', old.id, old.key, old.value);
    INSERT INTO memory_fts(rowid, key, value) VALUES (new.id, new.key, new.value);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, key, value) VALUES ('delete', old.id, old.key, old.value);
END;
"""


@dataclass
class MemoryEntry:
    memory_type: str
    key: str
    value: str
    tags: List[str] = field(default_factory=list)
    task_id: Optional[str] = None
    confidence: float = 1.0
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    accessed_at: Optional[str] = None
    access_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.memory_type,
            "key": self.key,
            "value": self.value,
            "tags": self.tags,
            "task_id": self.task_id,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "access_count": self.access_count,
        }


class MemoryStore:
    """
    Persistent cross-task memory for Forge agents.

    Usage:
        store = MemoryStore("forge_tasks.db")
        store.remember("preference", "code_style", "use black, 88 char lines")
        results = store.recall("code formatting")
    """

    def __init__(self, db_path: str = "forge_tasks.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()
        log.info("Memory store initialised", db=db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(MEMORY_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def remember(
        self,
        memory_type: str,
        key: str,
        value: str,
        tags: Optional[List[str]] = None,
        task_id: Optional[str] = None,
        confidence: float = 1.0,
    ) -> MemoryEntry:
        """
        Store or update a memory. If key exists, updates value and confidence.
        Returns the stored entry.
        """
        now = datetime.now(timezone.utc).isoformat()
        tags_json = json.dumps(tags or [])

        with self._lock:
            conn = self._conn()
            try:
                existing = conn.execute(
                    "SELECT id FROM memory_entries WHERE key=?", (key,)
                ).fetchone()

                if existing:
                    conn.execute("""
                        UPDATE memory_entries
                        SET value=?, memory_type=?, tags=?, confidence=?,
                            updated_at=?, accessed_at=?, access_count=access_count+1
                        WHERE key=?
                    """, (value, memory_type, tags_json, confidence, now, now, key))
                    entry_id = existing["id"]
                    log.info("Memory updated", key=key, type=memory_type)
                else:
                    cur = conn.execute("""
                        INSERT INTO memory_entries
                            (memory_type, key, value, tags, task_id, confidence,
                             created_at, updated_at, accessed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (memory_type, key, value, tags_json, task_id,
                          confidence, now, now, now))
                    entry_id = cur.lastrowid
                    log.info("Memory stored", key=key, type=memory_type)
                conn.commit()
            finally:
                conn.close()

        return MemoryEntry(
            id=entry_id, memory_type=memory_type, key=key, value=value,
            tags=tags or [], task_id=task_id, confidence=confidence,
            created_at=now, updated_at=now, accessed_at=now,
        )

    def recall(
        self,
        query: str,
        memory_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        max_results: int = _MAX_RESULTS,
    ) -> List[MemoryEntry]:
        """
        Retrieve memories matching a query using full-text search.
        Results are ranked by relevance then recency.
        """
        now = datetime.now(timezone.utc).isoformat()
        results = []

        with self._lock:
            conn = self._conn()
            try:
                # FTS5 query — escape special chars
                safe_query = query.replace('"', '""')
                fts_sql = """
                    SELECT e.*
                    FROM memory_entries e
                    JOIN memory_fts f ON f.rowid = e.id
                    WHERE memory_fts MATCH ?
                      AND e.archived = 0
                    ORDER BY rank, e.updated_at DESC
                    LIMIT ?
                """
                params: list = [safe_query, max_results]

                if memory_type:
                    fts_sql = fts_sql.replace(
                        "AND e.archived = 0",
                        "AND e.archived = 0 AND e.memory_type = ?"
                    )
                    params = [safe_query, memory_type, max_results]

                rows = conn.execute(fts_sql, params).fetchall()

                # Update access metadata
                for row in rows:
                    conn.execute("""
                        UPDATE memory_entries
                        SET accessed_at=?, access_count=access_count+1
                        WHERE id=?
                    """, (now, row["id"]))
                    entry = self._row_to_entry(row)
                    # Filter by tags if requested
                    if tags:
                        entry_tags = json.loads(row["tags"])
                        if not any(t in entry_tags for t in tags):
                            continue
                    results.append(entry)

                conn.commit()
            except sqlite3.OperationalError:
                # FTS query syntax error — fall back to LIKE search
                like_q = f"%{query}%"
                base_sql = """
                    SELECT * FROM memory_entries
                    WHERE archived=0 AND (key LIKE ? OR value LIKE ?)
                    ORDER BY updated_at DESC LIMIT ?
                """
                rows = conn.execute(base_sql, (like_q, like_q, max_results)).fetchall()
                results = [self._row_to_entry(r) for r in rows]
            finally:
                conn.close()

        return results

    def recall_all(
        self,
        memory_type: Optional[str] = None,
        task_id: Optional[str] = None,
        max_results: int = 50,
    ) -> List[MemoryEntry]:
        """Return all memories, optionally filtered by type or task."""
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM memory_entries WHERE archived=0"
                params = []
                if memory_type:
                    sql += " AND memory_type=?"
                    params.append(memory_type)
                if task_id:
                    sql += " AND task_id=?"
                    params.append(task_id)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(max_results)
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_entry(r) for r in rows]
            finally:
                conn.close()

    def forget(self, key: str) -> bool:
        """Archive (soft-delete) a memory by key."""
        with self._lock:
            conn = self._conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    "UPDATE memory_entries SET archived=1, updated_at=? WHERE key=?",
                    (now, key)
                )
                conn.commit()
                deleted = cur.rowcount > 0
                if deleted:
                    log.info("Memory archived", key=key)
                return deleted
            finally:
                conn.close()

    def forget_by_type(self, memory_type: str) -> int:
        """Archive all memories of a given type."""
        with self._lock:
            conn = self._conn()
            try:
                now = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    "UPDATE memory_entries SET archived=1, updated_at=? WHERE memory_type=?",
                    (now, memory_type)
                )
                conn.commit()
                log.info("Memories archived by type", type=memory_type, count=cur.rowcount)
                return cur.rowcount
            finally:
                conn.close()

    def archive_old(self) -> int:
        """Archive memories older than FORGE_MEMORY_MAX_AGE_DAYS."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)
        ).isoformat()
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE memory_entries SET archived=1 WHERE accessed_at < ? AND archived=0",
                    (cutoff,)
                )
                conn.commit()
                if cur.rowcount:
                    log.info("Old memories archived", count=cur.rowcount, cutoff=cutoff)
                return cur.rowcount
            finally:
                conn.close()

    def stats(self) -> dict:
        """Return memory statistics."""
        with self._lock:
            conn = self._conn()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE archived=0"
                ).fetchone()[0]
                by_type = {}
                for row in conn.execute(
                    "SELECT memory_type, COUNT(*) as c FROM memory_entries "
                    "WHERE archived=0 GROUP BY memory_type"
                ).fetchall():
                    by_type[row[0]] = row[1]
                return {"total": total, "by_type": by_type}
            finally:
                conn.close()

    def format_for_prompt(self, entries: List[MemoryEntry], max_chars: int = 2000) -> str:
        """Format memory entries as a compact string for injection into prompts."""
        if not entries:
            return ""
        lines = ["[Relevant memories from previous tasks:]"]
        total = 0
        for e in entries:
            line = f"  [{e.memory_type}] {e.key}: {e.value}"
            if total + len(line) > max_chars:
                lines.append("  ... (more memories truncated)")
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    @staticmethod
    def _row_to_entry(row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            memory_type=row["memory_type"],
            key=row["key"],
            value=row["value"],
            tags=json.loads(row["tags"]),
            task_id=row["task_id"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            accessed_at=row["accessed_at"],
            access_count=row["access_count"],
        )


# Module-level singleton — shared across all agents in a process
_store: Optional[MemoryStore] = None


def get_memory_store(db_path: str = "forge_tasks.db") -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore(db_path)
    return _store