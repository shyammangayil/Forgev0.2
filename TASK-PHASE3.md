# TASK-PHASE3 — Testing Suite

Parent: [TASK.md](TASK.md)

---

## Unit Tests

### `tests/unit/test_task_model.py`
- [ ] Task creation with defaults
- [ ] Valid state transitions (NEW→READY→IN_PROGRESS→VERIFIED→COMPLETE)
- [ ] Invalid state transitions rejected
- [ ] Parent cannot complete with incomplete children
- [ ] to_markdown() renders all fields
- [ ] to_dict() / from_dict() round-trip

### `tests/unit/test_config.py`
- [ ] Default config loads without env vars
- [ ] Env vars override defaults
- [ ] available_providers() returns only configured providers
- [ ] ensure_dirs() creates directories

### `tests/unit/test_router.py`
- [ ] Routing selects correct provider per task type
- [ ] Fallback chain triggered on provider failure
- [ ] Auth failures skip to next provider without retry
- [ ] Rate limit triggers exponential backoff
- [ ] Context length failure skips provider
- [ ] All providers exhausted raises ProviderError

### `tests/unit/test_security.py`
- [ ] OpenAI key pattern detected
- [ ] Anthropic key pattern detected
- [ ] AWS key pattern detected
- [ ] Hardcoded password detected
- [ ] Clean text returns no findings
- [ ] Prompt injection patterns detected
- [ ] Secret redaction in log output

### `tests/unit/test_repository.py`
- [ ] save() writes task to DB and markdown file
- [ ] get() returns saved task
- [ ] Sequence numbers auto-increment
- [ ] list_by_state() filters correctly
- [ ] save_checkpoint() / load_checkpoint() round-trip
- [ ] load_latest_checkpoint() returns most recent
- [ ] write_audit() / get_audit() round-trip
- [ ] stats() returns correct counts

## Integration Tests

### `tests/integration/test_orchestrator.py`
- [ ] Full lifecycle PLAN→COMPLETE with mock provider
- [ ] Failed EXECUTE phase transitions task to FAILED
- [ ] REJECTED review blocks VERIFY gate
- [ ] CRITICAL security blocks REVIEW gate
- [ ] FAIL QA blocks TEST gate
- [ ] Checkpoint saves after each phase
- [ ] Resume from checkpoint skips completed phases

### `tests/integration/test_agents.py`
- [ ] PlannerAgent produces structured JSON output
- [ ] ImplementerAgent detects placeholder code
- [ ] ReviewerAgent parses APPROVED / REJECTED
- [ ] SecurityAgent static scan finds fake secrets
- [ ] QAAgent maps criteria to PASS/FAIL

## End-to-End Tests

### `tests/e2e/test_cli.py`
- [ ] `forge --help` exits 0
- [ ] `forge health` runs without API keys (all show UNAVAILABLE)
- [ ] `forge list` shows empty table
- [ ] `forge run "test task"` creates task and shows ID
- [ ] `forge status <id>` shows task details
- [ ] `forge metrics` shows zeros when no tasks run
