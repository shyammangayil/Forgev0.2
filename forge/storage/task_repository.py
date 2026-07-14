"""
FORGE Task Repository
=====================
Durable persistence for tasks. Uses SQLite (stdlib) as the embedded database
with automatic migration to PostgreSQL when available. Every task is also
written as TASK-NNN.md for human readability and git tracking.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from forge.orchestration.task import Task, TaskState, TaskType
from forge.telemetry.logger import get_logger

log = get_logger("task_repository")


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    sequence_number INTEGER,
    data_json TEXT NOT NULL,
    state TEXT NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_sequence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_value INTEGER NOT NULL DEFAULT 1
);
INSERT OR IGNORE INTO task_sequence (id, next_value) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id TEXT NOT NULL,
    step TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, step)
);
CREATE TABLE IF NOT EXISTS audit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event TEXT NOT NULL,
    agent TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_records(task_id);
"""


class TaskRepository:
    def __init__(self, db_path: str = "forge_tasks.db", task_dir: Path = Path("TASKS")):
        self.db_path = db_path
        self.task_dir = task_dir
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        log.info("Task repository initialised", db=db_path, task_dir=str(task_dir))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(DB_SCHEMA)

    def _next_sequence(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT next_value FROM task_sequence WHERE id=1").fetchone()
        seq = row["next_value"]
        conn.execute("UPDATE task_sequence SET next_value=? WHERE id=1", (seq + 1,))
        return seq

    def delete(self, task_id: str) -> bool:
        """
        Hard-delete a task and all its checkpoints and audit entries.
        Used by the privacy gateway to remove a task that was blocked at
        creation time before it was ever executed.
        Returns True if the task existed and was deleted.
        """
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                conn.execute("DELETE FROM checkpoints WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM audit_log WHERE task_id=?", (task_id,))
                deleted = cur.rowcount > 0
        if deleted:
            log.info("Task deleted", task_id=task_id)
        return deleted

    def save(self, task: Task) -> Task:
        with self._lock:
            with self._conn() as conn:
                # Assign sequence number on first save
                if task.sequence_number is None:
                    task.sequence_number = self._next_sequence(conn)

                data_json = json.dumps(task.to_dict(), default=str)
                conn.execute("""
                    INSERT INTO tasks (id, sequence_number, data_json, state, parent_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        data_json=excluded.data_json,
                        state=excluded.state,
                        updated_at=excluded.updated_at
                """, (
                    task.id,
                    task.sequence_number,
                    data_json,
                    task.state.value,
                    task.parent_id,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ))

            # Write markdown file
            self._write_markdown(task)
            return task

    def _write_markdown(self, task: Task):
        seq = f"TASK-{task.sequence_number:03d}" if task.sequence_number else f"TASK-{task.id}"
        path = self.task_dir / f"{seq}.md"
        path.write_text(task.to_markdown(), encoding="utf-8")

    def get(self, task_id: str) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            return Task.from_dict(json.loads(row["data_json"]))

    def get_by_sequence(self, seq: int) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM tasks WHERE sequence_number=?", (seq,)).fetchone()
            if not row:
                return None
            return Task.from_dict(json.loads(row["data_json"]))

    def list_all(self) -> List[Task]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data_json FROM tasks ORDER BY sequence_number").fetchall()
            return [Task.from_dict(json.loads(r["data_json"])) for r in rows]

    def list_by_state(self, state: TaskState) -> List[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data_json FROM tasks WHERE state=? ORDER BY sequence_number",
                (state.value,)
            ).fetchall()
            return [Task.from_dict(json.loads(r["data_json"])) for r in rows]

    def list_children(self, parent_id: str) -> List[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data_json FROM tasks WHERE parent_id=? ORDER BY sequence_number",
                (parent_id,)
            ).fetchall()
            return [Task.from_dict(json.loads(r["data_json"])) for r in rows]

    def save_checkpoint(self, task_id: str, step: str, data: dict):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO checkpoints (task_id, step, data_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id, step) DO UPDATE SET data_json=excluded.data_json, created_at=excluded.created_at
            """, (task_id, step, json.dumps(data, default=str), datetime.now(timezone.utc).isoformat()))

    def load_checkpoint(self, task_id: str, step: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM checkpoints WHERE task_id=? AND step=?",
                (task_id, step)
            ).fetchone()
            return json.loads(row["data_json"]) if row else None

    def load_latest_checkpoint(self, task_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM checkpoints WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,)
            ).fetchone()
            return json.loads(row["data_json"]) if row else None

    def write_audit(self, task_id: str, event: str, agent: str = "", details: dict = None):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO audit_records (task_id, event, agent, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id, event, agent,
                json.dumps(details or {}, default=str),
                datetime.now(timezone.utc).isoformat()
            ))

    def get_audit(self, task_id: str) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_records WHERE task_id=? ORDER BY created_at",
                (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM tasks").fetchone()["c"]
            by_state = {}
            for row in conn.execute("SELECT state, COUNT(*) as c FROM tasks GROUP BY state").fetchall():
                by_state[row["state"]] = row["c"]
            return {"total": total, "by_state": by_state}
