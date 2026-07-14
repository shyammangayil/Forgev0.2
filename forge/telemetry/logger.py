"""
FORGE Structured Logger
=======================
Every log entry includes: timestamp, correlation_id, task_id, agent, event,
severity, duration, outcome. Output is JSON for easy ingestion.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

# Context vars propagated across async call stacks
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
_task_id: ContextVar[str] = ContextVar("task_id", default="")
_agent_name: ContextVar[str] = ContextVar("agent_name", default="system")


def set_correlation_id(cid: str):
    _correlation_id.set(cid)


def set_task_id(tid: str):
    _task_id.set(tid)


def set_agent(name: str):
    _agent_name.set(name)


def new_correlation_id() -> str:
    cid = str(uuid.uuid4())[:8]
    _correlation_id.set(cid)
    return cid


class ForgeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": _correlation_id.get() or "—",
            "task_id": _task_id.get() or "—",
            "agent": _agent_name.get() or "system",
            "event": record.getMessage(),
            "severity": record.levelname,
            "logger": record.name,
        }
        # Merge any extra structured fields
        for key in ("duration_ms", "outcome", "provider", "model", "tokens", "error"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry)


class ForgeLogger:
    """Thin wrapper that attaches structured fields to log records."""

    def __init__(self, name: str):
        self._log = logging.getLogger(f"forge.{name}")

    def _emit(self, level: int, msg: str, **kwargs):
        if self._log.isEnabledFor(level):
            record = self._log.makeRecord(
                self._log.name, level, "(forge)", 0, msg, (), None
            )
            for k, v in kwargs.items():
                setattr(record, k, v)
            self._log.handle(record)

    def info(self, msg: str, **kwargs):
        self._emit(logging.INFO, msg, **kwargs)

    def debug(self, msg: str, **kwargs):
        self._emit(logging.DEBUG, msg, **kwargs)

    def warning(self, msg: str, **kwargs):
        self._emit(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs):
        self._emit(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs):
        self._emit(logging.CRITICAL, msg, **kwargs)

    @contextmanager
    def timed(self, event: str, **kwargs):
        """Context manager that logs an event with elapsed time and outcome."""
        start = time.monotonic()
        try:
            yield
            elapsed = int((time.monotonic() - start) * 1000)
            self.info(event, duration_ms=elapsed, outcome="success", **kwargs)
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            self.error(event, duration_ms=elapsed, outcome="failure", error=str(exc), **kwargs)
            raise


def setup_logging(level: str = "INFO", fmt: str = "json"):
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(ForgeFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
        )

    # File handler
    import pathlib
    log_dir = pathlib.Path("logs")
    log_dir.mkdir(exist_ok=True)
    fh = logging.FileHandler(log_dir / "forge.log")
    fh.setFormatter(ForgeFormatter())

    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(fh)


def get_logger(name: str) -> ForgeLogger:
    return ForgeLogger(name)
