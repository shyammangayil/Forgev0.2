# TASK-PHASE2 — CLI & API Surface

Parent: [TASK.md](TASK.md)

---

## CLI (`forge/cli.py`)
- [~] `forge run <description>` — create and run a task through full lifecycle
- [~] `forge run --file <path>` — load task from markdown/text file
- [~] `forge status <task-id>` — show task state, phase, audit log
- [~] `forge list` — list all tasks with state and priority
- [~] `forge list --state IN_PROGRESS` — filter by state
- [~] `forge metrics` — show token usage, cost, latency stats
- [~] `forge health` — check all provider connectivity
- [~] `forge resume <task-id>` — resume a failed/blocked task from checkpoint
- [~] `forge audit <task-id>` — show full audit trail
- [~] Beautiful terminal output with ASCII tables and colour (stdlib only)

## REST API (`forge/api/server.py` + `forge/api/handlers.py`)
- [ ] `POST /api/tasks` — create task
- [ ] `GET  /api/tasks` — list tasks
- [ ] `GET  /api/tasks/{id}` — get task detail
- [ ] `POST /api/tasks/{id}/run` — run task
- [ ] `GET  /api/tasks/{id}/audit` — audit trail
- [ ] `GET  /api/metrics` — metrics snapshot
- [ ] `GET  /api/health` — provider health
- [ ] `GET  /metrics` — Prometheus text format
- [ ] CORS headers for browser access
- [ ] JSON error responses with status codes
- [ ] Request logging with correlation IDs

## Acceptance Criteria
- [ ] `python -m forge.cli --help` works with no API keys configured
- [ ] `forge health` shows status of all providers
- [ ] `forge list` shows empty table gracefully
- [ ] API server starts on port 8080 by default
- [ ] All endpoints return valid JSON
- [ ] Errors return structured JSON with `error` and `code` fields
