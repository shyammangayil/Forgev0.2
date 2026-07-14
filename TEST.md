# FORGE Test Documentation

Every test in the FORGE test suite is documented here with full specification.

**Total tests: 120 | All pass with no API keys required.**

Run all tests:
```bash
bash scripts/run_tests.sh
# or
python -m unittest discover tests/ -v
```

---

## TEST SUITE OVERVIEW

| Suite | File | Tests | Coverage |
|-------|------|-------|----------|
| Task Model | `tests/unit/test_task_model.py` | 33 | Task state machine, markdown, serialisation |
| Config / Security / Repo | `tests/unit/test_config_security_repo.py` | 43 | Config, secret scanning, SQLite persistence |
| Model Router | `tests/unit/test_router.py` | 16 | Routing, retry, failover |
| Orchestration Engine | `tests/integration/test_orchestrator.py` | 16 | Full lifecycle, gate failures, checkpoints |
| CLI E2E | `tests/e2e/test_cli.py` | 12 | CLI commands, exit codes, output |

---

## UNIT TESTS — Task Model (`test_task_model.py`)

---

### TEST-001: Default state is NEW
- **Objective**: Newly created tasks must start in NEW state
- **Preconditions**: None
- **Test Steps**: Create `Task(title="T", description="D")`; assert `task.state == TaskState.NEW`
- **Expected Result**: State is `NEW`
- **Success Criteria**: Assertion passes
- **Failure Criteria**: State is anything other than `NEW`
- **Status**: PASS

### TEST-002: Default type is CODING
- **Objective**: Tasks default to CODING type when not specified
- **Test Steps**: Create task without `task_type`; assert `task.task_type == TaskType.CODING`
- **Expected Result**: Type is `CODING`
- **Status**: PASS

### TEST-003: ID auto-generated, unique
- **Objective**: Each task gets a unique 8-character ID
- **Test Steps**: Create two tasks; assert IDs differ and have length 8
- **Expected Result**: `t1.id != t2.id`, `len(t1.id) == 8`
- **Status**: PASS

### TEST-004: Timestamps set on creation
- **Objective**: `created_at` and `updated_at` are set automatically
- **Test Steps**: Create task; assert both timestamps are not None
- **Status**: PASS

### TEST-005: NEW → READY transition succeeds
- **Objective**: Valid forward transition works
- **Test Steps**: Create task; call `task.transition(TaskState.READY)`; assert returns `True` and state is `READY`
- **Status**: PASS

### TEST-006: NEW → FAILED transition succeeds
- **Objective**: Tasks can be failed from NEW state
- **Status**: PASS

### TEST-007: Full happy-path transitions
- **Objective**: `NEW → READY → IN_PROGRESS → VERIFIED → COMPLETE` all succeed
- **Test Steps**: Apply each transition in order; assert each returns `True`
- **Expected Result**: Final state is `COMPLETE`
- **Status**: PASS

### TEST-008: Invalid transition rejected
- **Objective**: Skipping states is rejected; state unchanged
- **Test Steps**: Create task (NEW); try `transition(COMPLETE)`; assert returns `False`; assert state still `NEW`
- **Expected Result**: Returns `False`, state unchanged
- **Failure Criteria**: Invalid transition accepted
- **Status**: PASS

### TEST-009: COMPLETE is terminal
- **Objective**: No transitions are possible from COMPLETE
- **Test Steps**: Drive task to COMPLETE; attempt any transition; all return `False`
- **Status**: PASS

### TEST-010: FAILED can retry to READY
- **Objective**: Failed tasks can be retried
- **Test Steps**: Transition to FAILED; then to READY; assert succeeds
- **Status**: PASS

### TEST-011: Transition appends audit log entry
- **Objective**: Every state change is recorded
- **Test Steps**: Transition NEW→READY with reason "initiating"; check `audit_log[0]` has correct from/to/reason
- **Status**: PASS

### TEST-012: IN_PROGRESS sets started_at
- **Objective**: Execution start time recorded
- **Test Steps**: Transition to IN_PROGRESS; assert `task.started_at` is not None
- **Status**: PASS

### TEST-013: COMPLETE sets completed_at
- **Objective**: Completion time recorded
- **Test Steps**: Drive to COMPLETE; assert `task.completed_at` is not None
- **Status**: PASS

### TEST-014: can_complete() only true in VERIFIED
- **Objective**: Gate preventing premature completion
- **Test Steps**: Check `can_complete()` at NEW, IN_PROGRESS, VERIFIED states
- **Expected Result**: Only `True` at VERIFIED
- **Status**: PASS

### TEST-015–017: Markdown rendering
- **Objective**: `to_markdown()` includes all key fields
- **Test Steps**: Create task with title, criteria, result; call `to_markdown()`; assert each field appears in output
- **Status**: PASS (3 tests)

### TEST-018–022: Serialisation round-trip
- **Objective**: `to_dict()` / `from_dict()` preserve all fields
- **Test Steps**: Create fully-populated task; serialise; deserialise; compare all fields
- **Status**: PASS (5 tests)

### TEST-023–025: Parent/child blocking
- **Objective**: Parent task blocked by incomplete children
- **Test Steps**: Create parent with child reference; check `is_blocked_by_children()`
- **Expected Result**: `True` when child is incomplete; `False` when complete or no children
- **Status**: PASS (3 tests)

---

## UNIT TESTS — Config (`test_config_security_repo.py`)

### TEST-026: Default config loads without env vars
- **Objective**: FORGE starts without any environment configuration
- **Test Steps**: Instantiate `ForgeConfig()`; assert sub-configs are not None
- **Status**: PASS

### TEST-027: All 5 providers present by default
- **Objective**: anthropic, openai, gemini, ollama, openrouter all configured
- **Status**: PASS

### TEST-028: Env var overrides default provider
- **Test Steps**: Set `FORGE_CODING_PROVIDER=ollama`; load config; assert router uses ollama for coding
- **Status**: PASS

### TEST-029: Env var overrides fallback chain
- **Test Steps**: Set `FORGE_FALLBACK_CHAIN=ollama,openai`; assert chain is `["ollama", "openai"]`
- **Status**: PASS

### TEST-030: ensure_dirs() creates directories
- **Test Steps**: Set paths to temp dirs; call `ensure_dirs()`; assert all exist
- **Status**: PASS

### TEST-031: Ollama always available without key
- **Objective**: Offline operation always possible
- **Status**: PASS

---

## UNIT TESTS — Security Scanner

### TEST-032: OpenAI key pattern detected
- **Test Steps**: Pass `'key = "sk-abcdef..."'` to `scan_for_secrets()`; assert finding returned
- **Status**: PASS

### TEST-033: Anthropic key pattern detected
- **Test Steps**: Pass `sk-ant-...` pattern; assert detected
- **Status**: PASS

### TEST-034: AWS key pattern detected
- **Test Steps**: Pass `AKIA...` pattern; assert detected
- **Status**: PASS

### TEST-035: Clean text returns no findings
- **Test Steps**: Pass normal Python code; assert empty result
- **Status**: PASS

### TEST-036: os.environ.get() reference not flagged
- **Objective**: Reading from env vars is safe — not a hardcoded secret
- **Test Steps**: Pass `api_key = os.environ.get("ANTHROPIC_API_KEY")`; assert no finding
- **Status**: PASS

### TEST-037–040: Prompt injection detection
- **Objective**: "ignore previous instructions", "jailbreak" etc. detected
- **Status**: PASS (4 tests)

### TEST-041: Secret redaction in log output
- **Test Steps**: Pass text with OpenAI key to `redact_secrets_from_log()`; assert `[REDACTED]` in output
- **Status**: PASS

---

## UNIT TESTS — Task Repository

### TEST-042: save() and get() round-trip
- **Test Steps**: Save task; get by ID; assert title and state preserved
- **Status**: PASS

### TEST-043: Sequence numbers auto-increment
- **Test Steps**: Save 3 tasks; assert sequence numbers are 1, 2, 3
- **Status**: PASS

### TEST-044: get_by_sequence() works
- **Status**: PASS

### TEST-045: get() nonexistent returns None
- **Status**: PASS

### TEST-046: State update persisted
- **Test Steps**: Save task; transition state; save again; get; assert new state
- **Status**: PASS

### TEST-047: list_all() returns all tasks ordered by sequence
- **Status**: PASS

### TEST-048: list_by_state() filters correctly
- **Test Steps**: Save 2 tasks; advance one to READY; list READY and NEW separately
- **Status**: PASS

### TEST-049: TASK-NNN.md written on save
- **Test Steps**: Save task; assert markdown file exists with correct content
- **Status**: PASS

### TEST-050: Markdown file updated on state change
- **Status**: PASS

### TEST-051–053: Checkpointing
- save_checkpoint / load_checkpoint round-trip
- Overwrite updates value
- Non-existent checkpoint returns None
- **Status**: PASS (3 tests)

### TEST-054–055: Audit trail
- write_audit / get_audit round-trip
- Multiple records preserved in order
- **Status**: PASS (2 tests)

### TEST-056: stats() returns correct counts
- **Status**: PASS

### TEST-057: list_children() returns child tasks
- **Status**: PASS

---

## UNIT TESTS — Model Router (`test_router.py`)

### TEST-058: Coding tasks route to Anthropic
- **Objective**: Default routing policy maps CODING→anthropic
- **Status**: PASS

### TEST-059: Review tasks route to OpenAI
- **Status**: PASS

### TEST-060: Large-context tasks route to Gemini
- **Status**: PASS

### TEST-061: Primary provider is first in fallback chain
- **Status**: PASS

### TEST-062: Successful completion returns response
- **Test Steps**: Mock provider returns success; call `router.complete()`; assert content returned
- **Status**: PASS

### TEST-063: Provider called once on success (no unnecessary retries)
- **Status**: PASS

### TEST-064: Rate limit triggers retry with backoff
- **Test Steps**: Mock provider fails twice with RateLimitError then succeeds; assert called 3 times
- **Status**: PASS

### TEST-065: Transient error triggers retry
- **Test Steps**: Mock fails once with retryable ProviderError then succeeds; assert called twice
- **Status**: PASS

### TEST-066: Auth failure skips to next provider without retry
- **Objective**: Don't waste retries on authentication failures
- **Test Steps**: Primary mock raises AuthError; secondary mock succeeds; assert primary called once, response from secondary
- **Status**: PASS

### TEST-067: Context length failure skips to next provider
- **Status**: PASS

### TEST-068: All providers fail raises ProviderError
- **Test Steps**: All mocks fail; assert `ProviderError` raised with "All providers exhausted"
- **Status**: PASS

### TEST-069: Failover resets model for next provider
- **Objective**: Don't send Claude model name to OpenAI endpoint
- **Status**: PASS

### TEST-070–071: Health status aggregation
- **Status**: PASS (2 tests)

---

## INTEGRATION TESTS — Orchestration Engine (`test_orchestrator.py`)

### TEST-072: Task created with NEW state
- **Objective**: create_task() initialises task correctly
- **Preconditions**: Engine initialised with mock router
- **Status**: PASS

### TEST-073: Task persisted to database immediately
- **Test Steps**: Call `create_task()`; call `repo.get(task.id)`; assert not None
- **Status**: PASS

### TEST-074: TASK-NNN.md created on task creation
- **Status**: PASS

### TEST-075: Full lifecycle PLAN→COMPLETE succeeds
- **Preconditions**: Mock router returns valid responses for all 6 agent calls
- **Test Steps**: Create task; call `run_task()`; assert `result.success == True`
- **Expected Result**: All gates pass, result is success
- **Status**: PASS

### TEST-076: Task state is COMPLETE after success
- **Test Steps**: Run task; fetch from DB; assert state == COMPLETE
- **Expected Result**: `TaskState.COMPLETE`
- **Failure Criteria**: State remains NEW, IN_PROGRESS, etc.
- **Status**: PASS *(Bug fixed: required NEW→READY→IN_PROGRESS transition)*

### TEST-077: Result contains implementation
- **Test Steps**: Run task; assert `result.output["implementation"]` contains code
- **Status**: PASS

### TEST-078: Checkpoints saved for each phase
- **Test Steps**: Run task; assert checkpoint exists for `pre_PLAN`
- **Status**: PASS

### TEST-079: Audit records written
- **Test Steps**: Run task; call `repo.get_audit()`; assert `execution_started` event present
- **Status**: PASS

### TEST-080: Metrics updated after run
- **Test Steps**: Read `forge_tasks_created_total` before and after; assert incremented
- **Status**: PASS

### TEST-081: list_tasks() returns created tasks
- **Status**: PASS

### TEST-082: REJECTED review fails task
- **Preconditions**: Mock reviewer returns `{"verdict": "REJECTED", ...}`
- **Test Steps**: Run task; assert `result.success == False`; assert `"VERIFY"` in error message
- **Expected Result**: Task fails at VERIFY gate
- **Status**: PASS

### TEST-083: CRITICAL security fails task
- **Preconditions**: Mock security returns `{"verdict": "CRITICAL", ...}`
- **Test Steps**: Run task; assert fails at REVIEW gate
- **Status**: PASS

### TEST-084: QA FAIL fails task
- **Preconditions**: Mock QA returns `{"verdict": "FAIL", ...}`
- **Status**: PASS

### TEST-085: Task state is FAILED on provider error
- **Preconditions**: Mock router raises ProviderError
- **Test Steps**: Run task; fetch from DB; assert state == FAILED
- **Status**: PASS

### TEST-086: Secrets in implementation fail security gate
- **Preconditions**: Implementation contains `sk-ant-...` pattern
- **Test Steps**: Run task; assert security gate fails (static scan, not LLM)
- **Status**: PASS

### TEST-087: Checkpoint save/load round-trip
- **Status**: PASS

---

## END-TO-END TESTS — CLI (`test_cli.py`)

All E2E tests run CLI via subprocess. No real API calls.

### TEST-088: `forge --help` exits 0
- **Preconditions**: Package installed or run via `python -m forge.cli`
- **Expected Result**: Exit code 0; "FORGE" in stdout
- **Status**: PASS

### TEST-089: `forge --version` exits 0
- **Expected Result**: "1.0.0" in output
- **Status**: PASS

### TEST-090: `forge run --help` exits 0
- **Status**: PASS

### TEST-091: `forge list --help` exits 0
- **Status**: PASS

### TEST-092: `forge health --help` exits 0
- **Status**: PASS

### TEST-093: `forge health` completes without API keys
- **Preconditions**: All API key env vars cleared
- **Test Steps**: Run `forge health`; assert exit code 0
- **Expected Result**: Shows provider table; no crash
- **Failure Criteria**: Exit code non-zero or exception traceback in output
- **Status**: PASS

### TEST-094: `forge health` shows all providers
- **Test Steps**: Run `forge health`; assert ANTHROPIC, OPENAI, GEMINI, OLLAMA in output
- **Status**: PASS

### TEST-095: `forge list` empty shows "No tasks" message
- **Preconditions**: Fresh temp database
- **Expected Result**: Exit 0; "No tasks" in output
- **Status**: PASS

### TEST-096: `forge list --state NONEXISTENT` exits non-zero
- **Expected Result**: Non-zero exit; error message
- **Status**: PASS

### TEST-097: `forge run --dry-run` creates task without executing
- **Test Steps**: Run `forge run "description" --dry-run`; assert TASK- ID in output; assert "dry-run" in output
- **Expected Result**: Task created, not executed
- **Status**: PASS

### TEST-098: `forge run --file <path>` loads task from file
- **Preconditions**: Text file with task description created
- **Test Steps**: Run with `--file path --dry-run`; assert task created
- **Status**: PASS

### TEST-099: `forge run` without description exits non-zero
- **Expected Result**: Error message about missing description
- **Status**: PASS

---

## KNOWN LIMITATIONS

1. **E2E tests use real filesystem** — tests use `tempfile.mkdtemp()` for isolation; cleaned up in `tearDown`
2. **No real API calls in any test** — all LLM responses are mocked
3. **`forge metrics` test** — only checks output is non-empty; specific metric values depend on previous test runs in the same process
4. **`forge tasks`** — reads `TASK.md` from the repo root; if run from a different directory it will report file not found (expected)

---

## LIVE TEST — End-to-End FORGE Pipeline (Real Claude Opus)

**Task**: Build a Morning Workflow Dashboard in Streamlit
**Model**: claude-opus-4-5
**Date**: 2026-05-31
**Total time**: 251.4s | **LLM calls**: 6 | **Result**: SUCCESS

---

### TEST-100: PLAN phase — Planner Agent
- **Objective**: FORGE Planner decomposes the dashboard requirement into verifiable subtasks
- **Preconditions**: Task created with description and 8 acceptance criteria
- **Test Steps**: FORGE invokes Planner agent system prompt + user requirement; waits for JSON task breakdown
- **Expected Result**: Structured JSON with tasks array, each having acceptance_criteria
- **Duration**: 31.5s
- **Observed Result**: 6 tasks returned covering persistence, feeds, ideas, all 4 pages, main assembly
- **Status**: PASS ✅

---

### TEST-101: EXECUTE phase — Implementer Agent
- **Objective**: Implementer produces a complete, placeholder-free dashboard.py
- **Preconditions**: Plan phase passed; task context contains full description + criteria
- **Test Steps**: FORGE sends Implementer system prompt + full task spec; gate checks for TODO/FIXME/bare-ellipsis
- **Expected Result**: Working Python code in a ```python block, with Verification Steps section
- **Duration**: 97.6s
- **Observed Result**: 565-line dashboard.py with full implementations of all 8 modules. No placeholders. Gate PASS.
- **Evidence**: `tuple[str, ...]` type hint correctly NOT flagged after gate fix (regex `r"^\s+\.\.\.$"`)
- **Status**: PASS ✅

---

### TEST-102: VERIFY phase — Reviewer Agent (adversarial)
- **Objective**: Independent reviewer challenges implementation before it advances
- **Preconditions**: Implementation in ctx.phase_results["implementation"]
- **Test Steps**: FORGE sends Reviewer system prompt with full code; gate rejects if verdict == REJECTED
- **Expected Result**: JSON with verdict APPROVED|NEEDS_CHANGES, issues list, risk_level
- **Duration**: 26.0s
- **Observed Result**: `{"verdict":"APPROVED","risk_level":"LOW"}` — 2 LOW findings (html.escape, feed timeout), 0 HIGH/CRITICAL
- **Gate Action**: APPROVED → gate PASS
- **Status**: PASS ✅

---

### TEST-103: REVIEW phase — Security Agent (static scan + LLM)
- **Objective**: Security scan before code can advance to test phase
- **Preconditions**: Implementation in task context
- **Test Steps**: Static secret scan runs first (regex patterns); then Security LLM analysis; gate rejects on CRITICAL or secrets_detected
- **Expected Result**: JSON with verdict CLEAR|WARNINGS|CRITICAL; secrets_detected array
- **Duration**: 20.0s
- **Observed Result**: `{"verdict":"CLEAR","secrets_detected":[]}` — 2 INFO findings, no secrets found
- **Gate Action**: CLEAR + empty secrets → PASS
- **Status**: PASS ✅

---

### TEST-104: TEST phase — QA Agent
- **Objective**: All 8 acceptance criteria independently verified
- **Preconditions**: Review phase passed
- **Test Steps**: FORGE sends QA system prompt with criteria list + implementation; gate rejects if verdict == FAIL
- **Expected Result**: JSON with verdict PASS|PARTIAL|FAIL; criteria_results array; test_code
- **Duration**: 41.6s
- **Observed Result**: `{"verdict":"PASS"}` — all 8 criteria verified individually with specific code evidence
- **Generated pytest code**: 6 test functions covering persistence, round-trip, idea generation, atomic write
- **Status**: PASS ✅

---

### TEST-105: COMMIT phase — Documentation Agent
- **Objective**: Documentation produced before task marked COMPLETE
- **Preconditions**: All prior phases passed
- **Test Steps**: FORGE sends Documentation system prompt with implementation; no gate (docs always accepted)
- **Expected Result**: Module docstrings, ADRs, configuration reference
- **Duration**: 34.6s
- **Observed Result**: 3 ADRs (single-file, file persistence, template ideas), full module reference, security notes
- **Status**: PASS ✅

---

### TEST-106: Gate regression — execute gate ellipsis false positive (BUG FOUND + FIXED)
- **Objective**: Verify gate does not flag legitimate Python type hints
- **Test Steps**: Submit implementation containing `tuple[str, ...]`; verify gate accepts it
- **Expected Result**: Gate PASS (type hint ellipsis is not a placeholder)
- **Observed (before fix)**: Gate FAILED — `tuple[str, ...]` matched `"..."` substring check
- **Fix applied**: Gate now uses `re.search(r"^\s+\.\.\.\s*$", impl, re.MULTILINE)` — only matches bare ellipsis on its own line
- **Result after fix**: Gate correctly accepts type hints, correctly rejects placeholder bodies
- **Status**: PASS ✅ (after fix)

---

### TEST-107: Full test suite regression after live test changes
- **Objective**: Confirm all 122 existing tests still pass after gate fix
- **Test Steps**: `python -m unittest discover tests/ -v`
- **Expected Result**: 122 tests, 0 failures
- **Duration**: 3.5s
- **Observed Result**: Ran 122 tests in 3.544s — OK
- **Status**: PASS ✅
