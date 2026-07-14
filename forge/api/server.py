"""
FORGE REST API Server
======================
Lightweight HTTP server built on Python stdlib (http.server).
No FastAPI, Flask, or Django required.

Endpoints:
  POST /api/tasks              Create task
  GET  /api/tasks              List tasks
  GET  /api/tasks/{id}         Get task detail
  POST /api/tasks/{id}/run     Run task
  GET  /api/tasks/{id}/audit   Audit trail
  POST /api/tasks/{id}/resume  Resume task
  GET  /api/metrics            Metrics snapshot
  GET  /api/health             Provider health
  GET  /metrics                Prometheus text format
  GET  /api/status             API status
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from forge.config import get_config
from forge.orchestration.engine import OrchestrationEngine
from forge.orchestration.task import TaskState, TaskType
from forge.telemetry.logger import get_logger, new_correlation_id, setup_logging
from forge.telemetry.metrics import get_metrics

log = get_logger("api")
metrics = get_metrics()

# Global engine (initialised once)
_engine: Optional[OrchestrationEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> OrchestrationEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = OrchestrationEngine()
    return _engine


# ── Route table ───────────────────────────────────────────────────────────────

ROUTES = [
    ("POST", r"^/api/tasks$", "create_task"),
    ("GET",  r"^/api/tasks$", "list_tasks"),
    ("GET",  r"^/api/tasks/(?P<id>[^/]+)$", "get_task"),
    ("POST", r"^/api/tasks/(?P<id>[^/]+)/run$", "run_task"),
    ("POST", r"^/api/tasks/(?P<id>[^/]+)/resume$", "resume_task"),
    ("GET",  r"^/api/tasks/(?P<id>[^/]+)/audit$", "get_audit"),
    ("GET",  r"^/api/metrics$", "get_metrics_snapshot"),
    ("GET",  r"^/api/health$", "get_health"),
    ("GET",  r"^/api/status$", "get_status"),
    ("GET",  r"^/metrics$", "prometheus_metrics"),
]


# ── Handlers ──────────────────────────────────────────────────────────────────

def h_create_task(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """POST /api/tasks"""
    title = body.get("title", "").strip()
    description = body.get("description", "").strip()

    if not title:
        return 400, {"error": "title is required", "code": "MISSING_FIELD"}
    if not description:
        return 400, {"error": "description is required", "code": "MISSING_FIELD"}

    try:
        task_type = TaskType(body.get("type", "coding"))
    except ValueError:
        return 400, {"error": f"invalid task type", "code": "INVALID_TYPE",
                     "valid_types": [t.value for t in TaskType]}

    criteria = body.get("acceptance_criteria", [])
    priority = int(body.get("priority", 5))
    context = body.get("context", {})
    parent_id = body.get("parent_id")

    engine = get_engine()
    task = engine.create_task(
        title=title,
        description=description,
        task_type=task_type,
        acceptance_criteria=criteria,
        parent_id=parent_id,
        context=context,
        priority=max(1, min(10, priority)),
    )
    return 201, task.to_dict()


def h_list_tasks(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/tasks?state=&type=&limit="""
    engine = get_engine()
    state_filter = params.get("state", [None])[0]
    limit = int(params.get("limit", [100])[0])

    if state_filter:
        try:
            state = TaskState(state_filter.upper())
            tasks = engine.repo.list_by_state(state)
        except ValueError:
            return 400, {"error": f"Invalid state: {state_filter}"}
    else:
        tasks = engine.repo.list_all()

    tasks = tasks[:limit]
    return 200, {
        "tasks": [t.to_dict() for t in tasks],
        "count": len(tasks),
    }


def h_get_task(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/tasks/{id}"""
    task_id = path_params.get("id", "")
    engine = get_engine()
    task = engine.repo.get(task_id)

    if not task:
        # Try sequence number
        try:
            seq = int(task_id.lstrip("TASK-").lstrip("0") or "0")
            task = engine.repo.get_by_sequence(seq)
        except (ValueError, AttributeError):
            pass

    if not task:
        return 404, {"error": f"Task not found: {task_id}", "code": "NOT_FOUND"}

    return 200, task.to_dict()


def h_run_task(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """POST /api/tasks/{id}/run — runs synchronously (for demo; use async in production)"""
    task_id = path_params.get("id", "")
    engine = get_engine()
    task = engine.repo.get(task_id)

    if not task:
        return 404, {"error": f"Task not found: {task_id}", "code": "NOT_FOUND"}

    if task.state.value == "COMPLETE":
        return 409, {"error": "Task is already complete", "code": "ALREADY_COMPLETE"}

    # Run in a thread so we can return quickly (fire-and-forget for now)
    # In production, use a proper job queue
    async_mode = body.get("async", True)

    if async_mode:
        def _run():
            try:
                engine.run_task(task_id)
            except Exception as e:
                log.error("Background task run failed", task_id=task_id, error=str(e))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return 202, {
            "message": "Task execution started",
            "task_id": task_id,
            "status_url": f"/api/tasks/{task_id}",
        }
    else:
        try:
            result = engine.run_task(task_id)
            task_updated = engine.repo.get(task_id)
            return 200, {
                "success": result.success,
                "task": task_updated.to_dict() if task_updated else {},
                "error": result.error,
            }
        except Exception as e:
            return 500, {"error": str(e), "code": "EXECUTION_ERROR"}


def h_resume_task(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """POST /api/tasks/{id}/resume"""
    task_id = path_params.get("id", "")
    engine = get_engine()
    task = engine.repo.get(task_id)

    if not task:
        return 404, {"error": f"Task not found: {task_id}", "code": "NOT_FOUND"}

    resumable = {TaskState.FAILED, TaskState.BLOCKED, TaskState.IN_PROGRESS}
    if task.state not in resumable:
        return 409, {"error": f"Task cannot be resumed from state {task.state.value}"}

    task.transition(TaskState.READY, "API resume request")
    engine.repo.save(task)

    def _run():
        try:
            engine.run_task(task_id)
        except Exception as e:
            log.error("Resume run failed", task_id=task_id, error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return 202, {"message": "Task resumption started", "task_id": task_id}


def h_get_audit(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/tasks/{id}/audit"""
    task_id = path_params.get("id", "")
    engine = get_engine()
    audit = engine.repo.get_audit(task_id)
    return 200, {"task_id": task_id, "records": audit, "count": len(audit)}


def h_get_metrics_snapshot(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/metrics"""
    engine = get_engine()
    return 200, engine.metrics_summary()


def h_get_health(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/health"""
    engine = get_engine()
    status = engine.router.health_status()
    healthy = sum(1 for v in status.values() if v)
    overall = "healthy" if healthy > 0 else "degraded"
    return 200, {
        "status": overall,
        "providers": status,
        "healthy_count": healthy,
        "total_count": len(status),
    }


def h_get_status(body: dict, params: dict, path_params: dict) -> Tuple[int, Any]:
    """GET /api/status"""
    return 200, {
        "service": "FORGE",
        "version": "1.0.0",
        "status": "running",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def h_prometheus_metrics(body: dict, params: dict, path_params: dict) -> Tuple[int, str]:
    """GET /metrics — Prometheus text format"""
    return 200, get_metrics().prometheus_text()


HANDLER_MAP = {
    "create_task": h_create_task,
    "list_tasks": h_list_tasks,
    "get_task": h_get_task,
    "run_task": h_run_task,
    "resume_task": h_resume_task,
    "get_audit": h_get_audit,
    "get_metrics_snapshot": h_get_metrics_snapshot,
    "get_health": h_get_health,
    "get_status": h_get_status,
    "prometheus_metrics": h_prometheus_metrics,
}


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class ForgeHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default access log; we use structured logging

    def _send_json(self, status: int, data: Any):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Correlation-ID")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _handle(self, method: str):
        correlation_id = self.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])
        new_correlation_id()

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        start = time.monotonic()
        matched_handler = None
        path_params = {}

        for route_method, pattern, handler_name in ROUTES:
            if route_method != method:
                continue
            m = re.match(pattern, path)
            if m:
                matched_handler = handler_name
                path_params = m.groupdict()
                break

        if not matched_handler:
            self._send_json(404, {"error": f"No route for {method} {path}", "code": "NOT_FOUND"})
            return

        body = self._read_body() if method == "POST" else {}
        handler_fn = HANDLER_MAP.get(matched_handler)

        try:
            status, result = handler_fn(body, params, path_params)
            elapsed = int((time.monotonic() - start) * 1000)

            log.info("API request",
                     method=method, path=path, status=status,
                     duration_ms=elapsed, correlation_id=correlation_id)

            if isinstance(result, str):
                self._send_text(status, result)
            else:
                self._send_json(status, result)

        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            log.error("API handler error", path=path, error=str(exc), duration_ms=elapsed)
            self._send_json(500, {
                "error": "Internal server error",
                "code": "INTERNAL_ERROR",
                "detail": str(exc),
            })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Correlation-ID")
        self.end_headers()


# ── Server bootstrap ──────────────────────────────────────────────────────────

def run_server(host: str = "0.0.0.0", port: int = 8080):
    cfg = get_config()
    setup_logging(cfg.telemetry.log_level, cfg.telemetry.log_format)

    # Pre-warm the engine
    get_engine()

    server = HTTPServer((host, port), ForgeHTTPHandler)
    log.info("FORGE API server started", host=host, port=port)
    print(f"\n  FORGE API listening on http://{host}:{port}")
    print(f"  Health:  http://localhost:{port}/api/health")
    print(f"  Metrics: http://localhost:{port}/api/metrics")
    print(f"  Prometheus: http://localhost:{port}/metrics")
    print(f"\n  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server shutting down")
        server.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(port=port)
