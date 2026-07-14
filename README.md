# FORGE — Production-Grade AI Software Engineering Platform

> A resilient, multi-model, Level-5 autonomous AI engineering agent with zero trust in LLM output.

```
  ███████╗ ██████╗ ██████╗  ██████╗ ███████╗
  ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝
  █████╗  ██║   ██║██████╔╝██║  ███╗█████╗
  ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝
  ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗
  ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝
```

---

## What is FORGE?

FORGE is a self-contained AI software engineering platform. You give it a task. It plans, implements, validates, reviews, tests, and commits — using real tools, persistent memory, static code analysis, and external system integrations — and produces actual output files.

**Key capabilities:**

- Multi-agent pipeline: `[RESEARCH →] PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → COMMIT`
- 9 specialised agents with tool use: Planner, Implementer, Reviewer, QA, Security, Documentation, Recovery, Performance, Researcher
- 15 tools: web search, file I/O, git, bash, GitHub, Jira, Slack, file generation, memory
- AST-based static analysis: cyclomatic complexity, OWASP Top 10, CVE scanning, algorithmic complexity
- Cross-task persistent memory (SQLite FTS5 — no vector DB required)
- Cost optimisation: model selection, inference cache, token budget tracking
- Knowledge extraction: failure pattern detection, best practices, model profiling, playbooks
- **Self-healing Supervisor**: orchestrates the full repair loop — per-phase retry budgets, consensus scoring, repair-context injection, BLOCKED state management
- **Consensus gate**: weighted quality score across Reviewer + Security + QA must exceed threshold before COMMIT
- **Meta-learning loop**: LearningEngine runs automatically after every task; findings feed back into routing and prompt construction
- **Forge Thunderbird — Local LLM Stack**: run entirely offline via llama.cpp + llama-swap + LiteLLM; hot-swap between coding (Qwen3), reasoning (DeepSeek-R1), and vision (Qwen3VL) models without configuration; OpenAI-compatible API on `localhost:4000`
- Works fully offline with Ollama local models
- Resumes from checkpoints after crashes, timeouts, or machine restarts
- CLI, REST API, and Web UI (Gradio — see status below)

---

## Tool Maturity Level

```
Level 0: Chat only                    ✅
Level 1: Retrieval                    ✅  web_search, http_fetch, git_clone
Level 2: Compute                      ✅  bash (opt-in), search_files
Level 3: Create files                 ✅  write_file, generate_file (MD/CSV/XLSX/DOCX/PDF)
Level 4: Act on external systems      ✅  GitHub, Jira, Slack, memory store
Level 5: Autonomous re-planning       ✅  gate failure → rewind → re-plan with feedback
                                          ResearchAgent with iterative retrieval loop
                                          Failure pattern learning across task history
```

---

## Quickstart

```bash
git clone <repo> forge && cd forge
pip install -e .
cp .env.example .env        # edit — set at minimum one provider key
forge health                # verify providers
forge run "Build a REST API for user authentication with JWT tokens"
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CLI (forge)  │  REST API (:8080)  │  Web UI (Gradio — awaiting rework)  │
├──────────────────────────────────────────────────────────────────────────┤
│                        Orchestration Engine                               │
│  [RESEARCH] → PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → COMMIT        │
│  Dynamic re-planning  │  Checkpoint/resume  │  Slack notifications        │
├──────────────────┬───────────────────────────────────────────────────────┤
│  Teal Agents     │  Coral Agents       │  Researcher                      │
│  Planner         │  Security           │  (iterative web retrieval,       │
│  Implementer     │  Documentation      │   up to 12 tool rounds)          │
│  Reviewer        │  Recovery           │                                  │
│  QA              │  Performance        │                                  │
├──────────────────┴───────────────────────────────────────────────────────┤
│                     Static Analysis Gates (AST-based)                     │
│  ComplexityValidator  AlgorithmicValidator  ResourceValidator             │
│  ConcurrencyValidator  OWASPValidator  DependencyScanner                  │
│  AccessControlChecker  SecretsScanner                                    │
├──────────────────────────────────────────────────────────────────────────┤
│                       Tool Registry (15 tools)                            │
│  web_search  http_fetch  git_clone  read/write/list_files  bash           │
│  search_files  remember  recall  forget                                   │
│  github_read  github_write  jira  slack  generate_file                    │
├──────────────────────────────────────────────────────────────────────────┤
│                      Cost & Efficiency Layer                               │
│  ModelSelector (auto-routing)  InferenceCache  TokenBudgetTracker         │
├──────────────────────────────────────────────────────────────────────────┤
│                      Knowledge Extraction Layer                            │
│  FailurePatternDetector  BestPracticesExtractor                           │
│  ModelCapabilityProfiler  TaskPlaybookGenerator                           │
├──────┬──────────┬────────────┬──────────┬─────────────────────────────────┤
│Claude│  GPT-4o  │  Gemini    │  Ollama  │  OpenRouter                     │
├──────┴──────────┴────────────┴──────────┴─────────────────────────────────┤
│  SQLite  │  Memory Store (FTS5)  │  Structured JSON logging               │
│  TASKS/  │  workspace/           │  Prometheus-compatible metrics          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Pipeline phases

| Phase | Agent | Notes |
|-------|-------|-------|
| RESEARCH *(analysis tasks only)* | ResearchAgent | Pre-planning web retrieval. Up to 12 tool rounds. |
| PLAN | Planner | Decomposes task. Receives structured `repair_context` on retry. |
| EXECUTE | Implementer | Writes code. Performance validators run immediately after (AST-based). |
| VERIFY | Reviewer | Adversarial review. Gate failure classified as deterministic or transient. |
| REVIEW + TEST *(parallel, fail-fast)* | Security + QA | Security LLM verdict checked first; static OWASP scan only if verdict is safe. QA runs only after security clears. |
| COMMIT | Documentation | Generates docs, writes workspace files, persists memory, notifies Slack. |

### Self-healing execution loop (Supervisor)

The `Supervisor` is the conductor of the agent loop. It drives every task through the pipeline and handles all failure recovery:

```
PLAN → EXECUTE → VERIFY → [REVIEW ‖ TEST] → CONSENSUS → COMMIT
         ↑                                        |
         └──── REPAIR CONTEXT ←── ANALYZE FAILURE ┘
```

**Deterministic failures** (REJECTED, CRITICAL, FAIL verdicts) are not retried — the same agent with the same code will produce the same verdict.

**Transient failures** (LLM timeout, provider error, network failure) trigger the repair cycle: RecoveryAgent is invoked, a structured `repair_context` is injected into the task context, and the pipeline rewinds to PLAN.

### Retry budgets

Every task gets a fresh `RetryBudget` with independent per-phase counters. Counters are checkpointed and survive process restarts.

| Budget | Default | Env var |
|--------|---------|---------|
| Implementation retries | 5 | `FORGE_MAX_IMPL_RETRIES` |
| Review retries | 3 | `FORGE_MAX_REVIEW_RETRIES` |
| Test repair attempts | 5 | `FORGE_MAX_TEST_RETRIES` |
| Provider retries | 3 | `FORGE_MAX_PROVIDER_RETRIES` |
| Total loop iterations | 20 | `FORGE_MAX_LOOP_ITERATIONS` |

When a budget is exhausted, the task transitions to `BLOCKED` (not `FAILED`). A BLOCKED task can be resumed once the underlying issue is resolved: `forge resume TASK-NNN`.

### Consensus gate

Before COMMIT, a weighted quality score is computed across the three verification agents:

| Agent | Weight |
|-------|--------|
| Reviewer | 0.40 |
| Security | 0.35 |
| QA | 0.25 |

Verdicts map to scores: `APPROVED/CLEAR/PASS → 1.0`, `NEEDS_CHANGES/WARNINGS/PARTIAL → 0.5`, `REJECTED/CRITICAL/FAIL → 0.0`. The weighted sum must meet or exceed `FORGE_CONSENSUS_THRESHOLD` (default `0.65`) to allow COMMIT. Below threshold, the Supervisor triggers a repair cycle.

### Meta-learning loop

`LearningEngine.run_full_analysis()` runs automatically after every completed task (not just on manual `forge learn analyse`). Findings are persisted to the memory store and fed back into:
- `ModelRouter` — routing decisions use historical provider performance per task type
- Agent prompts — failure patterns and best practices are injected via `_inject_memories`

---

## CLI Reference

```bash
# Run tasks
forge run "description"
forge run "desc" --type analysis          # triggers RESEARCH phase first
forge run "desc" --criteria "c1;c2"
forge run --file task.md --dry-run
forge run "desc" --output ~/my-project/   # save workspace to custom directory
forge run "desc" --repo ./my-codebase     # attach repo for AS-IS analysis (sets repo_path context)
forge run "desc" --output ~/out --repo ./src --type analysis  # combined

# Inspect tasks
forge list                                # ID + TASK-NNN + state + title
forge list --state IN_PROGRESS
forge status TASK-013
forge show TASK-013                       # print workspace output files
forge show TASK-013 --file main.py
forge audit TASK-013

# Resume (all formats accepted)
forge resume TASK-013
forge resume 13
forge resume 0EAA494A

# Static analysis (no LLM — instant)
forge validate --file mycode.py
forge validate --file mycode.py --requirements requirements.txt

# Cost & token tracking
forge cost report                         # all-time summary by provider
forge cost report TASK-013                # cost breakdown for specific task
forge cost cache                          # inference cache stats
forge cost clear-cache                    # clear the cache
forge cost budget                         # show budget configuration

# Knowledge extraction & learning
forge learn report                        # full learning report
forge learn analyse                       # run analysis + persist to memory
forge learn playbooks                     # show generated task playbooks
forge learn profile                       # model capability profiles

# Memory
forge memory stats
forge memory list [--type TYPE]
forge memory recall "python async"
forge memory remember "key" "value" [--type TYPE]
forge memory forget "key"
forge memory clear --type error

# Platform
forge health                              # check all provider keys
forge metrics                             # token usage, costs, latency
forge tasks                               # TASK.md build progress

# Local LLM Stack (Forge Thunderbird)
forge stack start                        # scan models, generate configs, start stack
forge stack stop                         # stop llama-swap and LiteLLM
forge stack status                       # show running components and PIDs
forge stack models                       # list discovered GGUF files and role mappings
forge stack config                       # regenerate llama-swap + LiteLLM YAML configs
forge stack health                       # detailed health check with loaded model info

# Test Intelligence (pure static analysis — no LLM, instant)
forge intelligence scan myfile.py         # full 4-pass report
forge intelligence scan src/              # multi-file with aggregate summary
forge intelligence risks myfile.py        # risk areas only, coloured by severity
forge intelligence dead myfile.py         # dead code only
forge intelligence brief myfile.py        # print QA brief (as injected into QA agent)

# Repository Intelligence (reverse-engineer a codebase into an AS-IS prompt)
forge intelligence repo scan ./my-repo           # full architecture report
forge intelligence repo scan ./my-repo --as-is   # also print the LLM-ready AS-IS prompt
forge intelligence repo compare ./v1 ./v2        # side-by-side diff of two repos
forge intelligence repo brief ./my-repo          # print only the AS-IS prompt (pipe-friendly)

# Privacy gateway
forge privacy scan --file mycode.py       # scan file for PII/GDPR/HIPAA issues
forge privacy scan --text "..."           # scan text inline
forge privacy config                      # show gateway configuration
forge privacy test                        # run self-test suite (8 checks)
```

---

## Static Analysis Validators

All validators are AST-based — no LLM, no network, runs in milliseconds. They fire automatically after each task phase and can also be run standalone with `forge validate`.

### Performance validators (run after EXECUTE)

| Validator | Checks | Gate behaviour |
|-----------|--------|----------------|
| `ComplexityValidator` | Cyclomatic complexity, function length | Warn (set `FORGE_COMPLEXITY_GATE=fail` to block) |
| `AlgorithmicValidator` | O(n²) nested loops, sort-in-loop, string concat loop, list-in-loop | Warn on MEDIUM, fail on HIGH |
| `ResourceValidator` | Unclosed file handles, unbounded list growth | Warn |
| `ConcurrencyValidator` | Global mutation in threads, missing locks | Warn on MEDIUM, fail on HIGH |

### Security validators (REVIEW gate — replaces basic regex scanner)

| Validator | Checks | Gate behaviour |
|-----------|--------|----------------|
| `SecretsScanner` | 15 secret patterns: AWS keys, GitHub PATs, Slack tokens, private keys, DB connection strings | FAIL on any detection |
| `OWASPValidator` | SQL injection, command injection, weak crypto (MD5/SHA1/DES), hardcoded credentials, unprotected routes, unsafe deserialization, XXE | FAIL on CRITICAL/HIGH |
| `DependencyScanner` | Bundled CVE database (15 common packages). Optional OSV.dev API check via `FORGE_CVE_CHECK=true` | FAIL on HIGH/CRITICAL CVEs |
| `AccessControlChecker` | Sensitive operations without auth checks, IDOR patterns | FAIL on HIGH |

Tuning:
```ini
FORGE_COMPLEXITY_GATE=warn   # warn|fail
FORGE_SECRETS_GATE=fail      # warn|fail
FORGE_CVE_CHECK=false        # true enables online OSV.dev lookup
```

---

## Cost & Token Efficiency

### Model selection optimizer (off by default)

When `FORGE_COST_OPTIMIZE=true`, Forge automatically routes tasks to the cheapest model that meets quality requirements based on a complexity score (0-10) estimated from the task description.

| Complexity | Tier | Default providers (cheapest first) |
|------------|------|------------------------------------|
| 0-3 | minimal | Ollama → Gemini Flash → GPT-4o-mini |
| 4-6 | standard | Gemini Flash → GPT-4o → Claude Haiku |
| 7-8 | premium | Claude Sonnet → GPT-4o → Gemini Pro |
| 9-10 | maximum | Claude Opus → GPT o1 |

### Inference cache (off by default)

When `FORGE_CACHE_ENABLED=true`, identical LLM calls (same provider + model + prompt) return the cached response instantly at zero cost. Useful for development iteration and repeated tasks.

### Token budget tracker (always active)

Every LLM call is recorded with full attribution. Warns when a task exceeds configured budgets.

```ini
FORGE_COST_OPTIMIZE=false    # enable automatic model selection
FORGE_CACHE_ENABLED=false    # enable inference cache
FORGE_TOKEN_BUDGET=50000     # warn above this many tokens per task
FORGE_MAX_COST_USD=1.00      # warn above this cost per task
```

---

## Test Intelligence

FORGE Test Intelligence is a **pre-LLM static analysis engine** that identifies what needs testing — ranked by risk and business criticality — before the QA agent writes a single test case.

### Generation comparison

| Generation | Examples | How testing is decided |
|---|---|---|
| Gen-3: smart automation | Playwright, Cypress, Testim | Record/replay — automates whatever a developer points at. No concept of risk or coverage gaps. |
| Gen-4: AI-assisted | Momentic, Octomind, ACCELQ, GitHub Copilot agents | LLM decides what to click/assert from a description. Still reactive — someone must direct it. |
| **FORGE Test Intelligence** | This module | Proactively analyses the code graph before any test is written. Ranks every function by risk score. Tells the QA agent exactly what to test and why. |

### Four analysis passes (pure AST, zero LLM calls, < 5ms)

**Critical Paths** — Execution paths that, if broken, break everything. Scored by fan-in (35%), call depth (25%), cyclomatic complexity (20%), public API status (15%), exception-propagation potential (5%). The QA agent tests these first.

**Dead Code** — Code that can never execute: unreachable-after-return statements, always-false conditions (`if False:`), empty `except: pass` blocks, unused private functions, unused imports. The QA agent is told explicitly *not* to write tests for these.

**Risk Areas** — Eight detectors with calibrated scores:

| Risk | Score | Test hint |
|---|---|---|
| f-string SQL construction | 0.95 | SQL metacharacter injection |
| Unbounded recursion | 0.80 | Base-case input + depth limit |
| Bare `except:` | 0.75 | Verify specific exceptions propagate |
| Mutable default arguments | 0.70 | Call twice; assert no state leaks |
| High cyclomatic complexity | 0.5–1.0 | Equivalence partitioning per branch |
| Deep nesting (≥ 4) | 0.3–0.9 | Boundary conditions at each level |
| Missing input validation | 0.55 | None, empty, negative, out-of-range |
| Unsafe type coercion | 0.50 | Non-numeric string → ValueError |

**Untested Areas** — Functions with no existing test coverage signal (cross-referenced against `test_*.py` files). Public functions ranked before private. Context-sensitive test hints from function names: `parse_*` → malformed input, `validate_*` → valid+invalid, `delete_*` → idempotency, `auth_*` → invalid credentials.

### Integration

The engine runs automatically at two points in every task:

1. **After EXECUTE** — `OrchestrationEngine._execute_phase(EXECUTE)` analyses the implementation immediately. Summary stored in `ctx.phase_results["test_intelligence"]` and written to the audit log.
2. **Inside QAAgent.run()** — `to_qa_brief()` is injected into the QA agent's prompt before the LLM generates test cases. Risk findings are persisted to the cross-task memory store.

### Configuration

```bash
FORGE_TI_ENABLED=true              # enable/disable (default: true)
FORGE_RISK_THRESHOLD=0.5           # min score to persist to memory store
FORGE_MAX_COMPLEXITY=10            # cyclomatic complexity threshold
FORGE_DEAD_CODE_GATE=warn          # warn | fail
FORGE_TI_PERSIST_MEMORY=true       # persist findings to cross-task memory
FORGE_TI_MAX_CRITICAL_PATHS=5      # max paths in QA brief
FORGE_TI_MAX_RISK_AREAS=8          # max risk areas in QA brief
FORGE_TI_MAX_UNTESTED_AREAS=8      # max untested areas in QA brief
```

---

## Repository Intelligence

The Repository Intelligence Engine reverse-engineers an existing codebase into an **AS-IS prompt** — a self-contained, LLM-ready representation of the repository that captures its purpose, architecture, patterns, and tech stack. Inspired by the Loki framework's philosophy of deep structural understanding.

### What it produces

Given a repository directory, the engine produces a `RepositoryReport` containing:

| Dimension | What is captured |
|---|---|
| Purpose & Domain | One-line purpose inferred from README + entry-points + module names. Domain tags: AI/ML, web API, CLI tool, data, DevOps, etc. |
| Architecture Map | Layer diagram: api/routes, agents, llm/providers, tools, memory/storage, config, telemetry, auth, tests, utils. |
| Dependency Graph | Internal import relationships (who imports what) + external deps from requirements.txt / pyproject.toml. |
| Entry Points | CLI commands, `if __name__ == "__main__"` blocks, FastAPI/Flask routes, `__all__` exports. |
| Pattern Catalogue | Design patterns detected: Singleton, Factory, Observer/Event, Decorator, Registry, Strategy/Abstract, Pipeline. |
| Tech Stack | Frameworks, databases, infra — detected from both dependency files and actual import statements. |
| AS-IS Prompt | The distilled LLM-ready representation: what this codebase IS and how to work within it. |

### AS-IS prompt (the key output)

The AS-IS prompt is what makes this useful for FORGE tasks. It answers the question an LLM needs answered before modifying an existing codebase: *"What am I working with?"*

```
=== REPOSITORY AS-IS: my-service ===

PURPOSE: A production-grade AI agent framework with multi-LLM support

TECH STACK:
  Frameworks: FastAPI, Pydantic, asyncio, Anthropic SDK
  Databases:  SQLite, ChromaDB
  Key deps:   anthropic, fastapi, pydantic, sqlalchemy

ARCHITECTURE LAYERS:
  api / routes       myapp.api.routes, myapp.api.websocket
  agents             myapp.agents.base, myapp.agents.chat
  llm / providers    myapp.llm.base, myapp.llm.router
  memory / storage   myapp.memory.store, myapp.memory.manager
  config             myapp.config

ENTRY POINTS:
  • myapp.api.routes.health  [route/cmd]
  • myapp.cli  [__main__]

DESIGN PATTERNS DETECTED:
  • Registry  in myapp.tools.registry  [80%]
  • Singleton in myapp.agents.chat     [80%]
  • Strategy/Abstract in myapp.llm.base [78%]

=== END AS-IS ===
```

### Integration with FORGE

**`ResearchAgent.run()`** — When a task sets `context["repo_path"]` (via `forge run --repo ./path`), the ResearchAgent automatically runs the Repository Intelligence Engine first, injects the AS-IS prompt into its research context, and persists it to the cross-task memory store so the PlannerAgent can use it.

**`forge run --repo ./path`** — Attach a repository to any task. The AS-IS analysis runs before the RESEARCH phase and becomes part of the task context that flows through all agents.

### Comparison mode

```bash
forge intelligence repo compare ./v1 ./v2
```

Produces a side-by-side diff: frameworks added/removed, packages added/removed, new/lost design patterns, size delta (lines, classes, functions), and the full AS-IS for both versions.

### Configuration

```bash
FORGE_RI_MAX_FILES=200         # max Python files to deep-analyse
FORGE_RI_MAX_LINES=5000        # max lines per file before skipping AST
FORGE_RI_INCLUDE_TESTS=false   # include test files in analysis
```

---



All data flowing into and out of the LLM pipeline passes through the Privacy Gateway. No PII, PHI, or GDPR special-category data reaches any model in plaintext.

### How it works

Every LLM call in `BaseAgent._call_llm()` gets a unique `correlation_id`. The gateway uses this as the key for a per-request **redaction ledger**:

```
1. Outbound (before sending to LLM):
   alice@corp.com  →  «PII-EMAIL-0»
   123-45-6789     →  «PII-SSN-1»
   Manifest stored in ledger[correlation_id]

2. Inbound (after response received):
   Ledger[correlation_id] consumed (one-shot)
   Response scanned for residual PII not in the prompt
   Residual PII redacted
```

The same PII value always gets the same token within a request, so the model sees consistent placeholders.

### Detectors

| Detector | Covers | Action |
|----------|--------|--------|
| `PiiDetector` | Email, phone (US + intl), SSN, credit card, IBAN, passport, DOB, full name, postcode | Redact with typed token |
| `GdprClassifier` | GDPR Art.9: health, racial/ethnic, political, religious, union, biometric, sexual orientation, criminal, financial | Flag (contextual — not blindly redacted) |
| `HipaaDetector` | PHI: MRN, NPI, DEA, ICD-10, CPT codes, device IDs | Redact with typed token |
| `DataResidencyChecker` | Cross-border transfer mentions (when `FORGE_DATA_RESIDENCY` is set) | Flag |
| `PromptLeakDetector` | Instruction overrides, persona hijack, jailbreak, prompt exfiltration, template injection | Redact with typed token |

### Integration points

1. **`engine.create_task()`** — Scans title, description, context, acceptance criteria at task creation. With `FORGE_PRIVACY_GATE=fail`, hard-blocks (deletes task + raises `PrivacyViolationError`) before any LLM call.
2. **`BaseAgent._call_llm()`** — Scans system prompt and user prompt independently before sending to any provider. Scrubs the LLM response on receipt.

### Configuration

```bash
FORGE_PRIVACY_GATE=warn          # warn (default) | fail — block on HIGH findings
FORGE_PRIVACY_REDACT=true        # true (default) | false — replace PII with tokens
FORGE_DATA_RESIDENCY=            # EU | US | (empty — disabled)
FORGE_PRIVACY_LOG_LEVEL=summary  # summary (default) | full — per-finding detail
FORGE_PRIVACY_ENABLED=true       # true (default) | false — bypass entirely
FORGE_PRIVACY_LEDGER_TTL_S=300   # seconds before an unmatched ledger entry expires
```

---

## Knowledge Extraction & Learning

Analyses your full task history in `forge_tasks.db` with no LLM calls. Results are automatically persisted to the memory store so all agents benefit from them.

**What it extracts:**

| Subsystem | What it finds |
|-----------|---------------|
| `FailurePatternDetector` | Recurring failures by phase + error type, with fix suggestions |
| `BestPracticesExtractor` | Successful patterns, preferred providers, common criteria |
| `ModelCapabilityProfiler` | Empirical success rate, latency, cost per provider/model |
| `TaskPlaybookGenerator` | Reusable playbooks from completed tasks by type |

Run after accumulating a few tasks:
```bash
forge learn analyse    # analyse + persist findings to memory
forge learn report     # view full report
forge learn profile    # which models perform best for which tasks
```

---

## Tool Reference

### Retrieval
| Tool | Description | Requires |
|------|-------------|----------|
| `web_search` | DuckDuckGo (free) or Tavily | Tavily: `TAVILY_API_KEY` |
| `http_fetch` | Fetch URL up to 64KB | — |
| `git_clone` | Shallow-clone repo to workspace | git in PATH |

### Compute
| Tool | Description | Requires |
|------|-------------|----------|
| `bash` | Shell commands in workspace | `FORGE_ALLOW_BASH=true` |
| `search_files` | Regex search across workspace | — |

### File I/O
| Tool | Description | Optional deps |
|------|-------------|---------------|
| `read_file` | Read workspace file | — |
| `write_file` | Write workspace file | — |
| `list_files` | List directory | — |
| `generate_file` | MD / CSV / XLSX / DOCX / PDF | openpyxl, python-docx, reportlab |

### Memory
| Tool | Description |
|------|-------------|
| `remember` | Store fact / preference / finding / error / context |
| `recall` | FTS5 keyword search over all memories |
| `forget` | Archive a memory by key |

### External integrations
| Tool | Description | Requires |
|------|-------------|----------|
| `github_read` | Repo info, files, issues, PRs, code search | `GITHUB_TOKEN` |
| `github_write` | Create issues, PRs, branches, commit files | `GITHUB_TOKEN` |
| `jira` | Get / list / create issues, comment, transition | `JIRA_URL` + `JIRA_EMAIL` + `JIRA_API_TOKEN` |
| `slack` | Send messages, rich notifications, DMs, history | `SLACK_BOT_TOKEN` |

---

## Agent Tool Permissions

| Agent | Allowed Tools |
|-------|--------------|
| Planner | web_search, http_fetch, git_clone, list_files, read_file, search_files, recall, remember |
| Implementer | read_file, write_file, list_files, search_files, git_clone, bash, recall, remember, github_read, generate_file |
| Reviewer | read_file, list_files, search_files, recall, remember |
| QA | read_file, list_files, bash, recall, remember |
| Security | read_file, list_files, search_files, recall, remember |
| Researcher | web_search, http_fetch, git_clone, read_file, list_files, search_files, remember, recall |

Every agent receives **passive memory injection** — relevant memories from previous tasks are automatically prepended to the system prompt before every LLM call.

---

## Provider Configuration

| Provider | Key | Best For | Offline |
|----------|-----|----------|---------|
| Anthropic Claude | `ANTHROPIC_API_KEY` | Complex reasoning, coding | No |
| OpenAI GPT | `OPENAI_API_KEY` | Structured output, reviews | No |
| Google Gemini | `GOOGLE_API_KEY` | Large context tasks | No |
| Ollama | *(none)* | Fully offline / air-gapped | **Yes** |
| OpenRouter | `OPENROUTER_API_KEY` | Multi-model proxy | No |

### Ollama tuning (critical for large local models)

```ini
OLLAMA_BASE_URL=http://localhost:11434
FORGE_OLLAMA_MODEL=gpt-oss:20b

FORGE_OLLAMA_TIMEOUT=1800        # 30 min. Increase for 70B+
FORGE_MAX_RETRIES=1              # resume handles retries

# Context trimming — tune to model size
FORGE_CONTEXT_CHARS=4000         # 7B→2000  20B→4000  70B+→8000
FORGE_PLAN_CHARS=2000
```

### Routing

```ini
FORGE_DEFAULT_PROVIDER=ollama
FORGE_CODING_PROVIDER=ollama
FORGE_REVIEW_PROVIDER=ollama
FORGE_LARGE_CONTEXT_PROVIDER=ollama
FORGE_OFFLINE_PROVIDER=ollama
FORGE_FALLBACK_CHAIN=ollama
```

### Integration keys

```ini
GITHUB_TOKEN=ghp_...
JIRA_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=#forge-notifications
FORGE_SEARCH_PROVIDER=tavily     # duckduckgo if not set
TAVILY_API_KEY=tvly-...
FORGE_ALLOW_BASH=false
```

> **Important:** `.env` files do not support inline comments. Every `#` after
> a value is part of the value. Put comments on their own line.

---

## Output Files

| Location | Contents |
|----------|----------|
| `TASKS/TASK-NNN.md` | Metadata, description, criteria, full audit trail |
| `workspace/TASK-NNN/` | Extracted code files + `SUMMARY.md` |
| `forge_tasks.db` | All results, memory, cache, token usage (SQLite) |

```bash
forge show TASK-013                  # all workspace files
forge show TASK-013 --file main.py   # specific file
```

---

## Web UI

> ⚠️ **Status: Awaiting Rework**
>
> The Gradio UI (`forge/ui/gradio_app.py`) exists and launches, but is undergoing
> a full redesign to properly support Gradio 6.x. The current version may work
> partially but has known issues with streaming and component layout under
> Gradio 6.16+. Use the CLI or REST API for reliable operation until the
> rework is complete.

When the rework is complete the UI will provide:
- Provider + model selector sidebar
- Task type, priority, and acceptance criteria inputs
- Live pipeline status bar (PLAN → EXECUTE → VERIFY → REVIEW → TEST → COMMIT)
- Streaming progress updates during pipeline execution
- Review findings and code excerpt in final response
- Provider health, memory stats, and recent task history panels

To attempt running the current version:
```bash
python -m forge.ui.gradio_app   # http://127.0.0.1:7860
```

---

## Project Structure

```
forge/
├── forge/
│   ├── config.py                  # All config; ModelProfile, LocalStackConfig, PrivacyConfig
│   ├── cli.py                     # All CLI: run --output/--repo, stack, intelligence, privacy
│   ├── agents/agents.py           # 9 agents; ResearchAgent uses Repository Intelligence
│   ├── tools/registry.py          # 15 tools; _workspace() reads live config
│   ├── memory/store.py            # SQLite FTS5 cross-task memory store
│   ├── cost/optimizer.py          # ModelSelector, InferenceCache, TokenBudgetTracker
│   ├── validators/
│   │   ├── performance.py         # Complexity, algorithmic, resource (AST)
│   │   └── security.py            # OWASP, CVE, secrets (AST + regex)
│   ├── learning/patterns.py       # Failure patterns, best practices, model profiles
│   ├── local_stack/               # Forge Thunderbird — local LLM stack
│   │   ├── __init__.py
│   │   ├── model_registry.py      # GGUF discovery, role detection, mmproj association
│   │   ├── config_generator.py    # Auto-generates llama-swap + LiteLLM YAML
│   │   ├── health.py              # Liveness checks for all stack components
│   │   └── launcher.py            # Spawns + monitors llama-swap and LiteLLM
│   ├── intelligence/
│   │   ├── __init__.py            # Public API: test + repo intelligence exports
│   │   ├── analyser.py            # TestIntelligenceEngine — 4-pass AST analysis
│   │   └── repository.py          # RepositoryIntelligenceEngine — AS-IS prompt
│   ├── privacy/
│   │   ├── __init__.py
│   │   └── gateway.py             # Privacy Gateway: detectors, correlation-id ledger
│   ├── integrations/
│   │   ├── github.py, jira.py, slack.py
│   │   └── files.py               # MD/CSV/XLSX/DOCX/PDF; _workspace() reads live config
│   ├── orchestration/
│   │   ├── engine.py              # Full lifecycle; runs Test Intelligence after EXECUTE
│   │   ├── supervisor.py          # Supervisor: repair loop, budgets, consensus, meta-learning
│   │   ├── budgets.py             # RetryBudget: per-phase independent counters
│   │   └── task.py                # Task model and state machine (incl. BLOCKED state)
│   ├── providers/base.py          # 6 providers: Anthropic, OpenAI, Gemini, Ollama,
│   │                              #   OpenRouter, LiteLLM (local stack, zero cost)
│   ├── router/model_router.py     # Task-type routing + role alias for litellm
│   ├── storage/task_repository.py # SQLite + TASK-NNN.md
│   ├── telemetry/                 # Structured JSON logging + Prometheus metrics
│   ├── api/server.py              # REST API (stdlib only)
│   └── ui/gradio_app.py           # ⚠️ Web UI — awaiting Gradio 6 rework
├── tests/
│   ├── unit/                      # 406 tests — pure stdlib unittest, no pytest
│   │   ├── test_local_stack.py           # Forge Thunderbird (44 tests)
│   │   ├── test_intelligence.py          # Test Intelligence Engine (52 tests)
│   │   ├── test_repository_intelligence.py  # Repository Intelligence (53 tests)
│   │   ├── test_privacy_gateway.py       # Privacy Gateway (40 tests)
│   │   └── test_budgets_supervisor.py    # Supervisor + budgets (11 tests)
│   ├── integration/               # 16 tests — full engine with mocked providers
│   └── e2e/                       # 14 tests — CLI subprocess tests
├── scripts/
│   ├── run_tests.sh               # ./scripts/run_tests.sh [unit|integration|e2e|all]
│   └── run_tests.ps1              # .\scripts\run_tests.ps1 [-Suite unit|integration|e2e|all]
├── llama_swap_config.yaml         # Auto-generated by forge stack start (do not commit)
├── litellm_config.yaml            # Auto-generated by forge stack start (do not commit)
├── .forge_stack_pids.json         # Runtime PID file for forge stack stop (do not commit)
├── TASKS/                         # TASK-NNN.md summaries (auto-generated)
├── workspace/                     # Default output dir; override with FORGE_WORKSPACE or --output
├── forge_tasks.db                 # SQLite — tasks, memory, cache, usage (auto-generated)
├── .env                           # Your config (never commit)
├── .env.example                   # Template including Forge Thunderbird settings
├── requirements.txt
└── pyproject.toml
```

---

## Optional Dependencies

```bash
pip install openpyxl       # XLSX generation
pip install python-docx    # DOCX generation
pip install reportlab      # PDF (falls back to print-ready HTML without it)
```

---

## Forge Thunderbird — Local LLM Stack

Forge Thunderbird is the local inference layer of Forge. It lets you run the full agent pipeline — Planner, Implementer, Reviewer, QA, Security — entirely offline using your own GPU and GGUF models. No API keys, no data leaving your machine, no per-token cost.

### Architecture

```
forge run "..."
    ↓
Forge Router  (selects provider by task type)
    ↓
LiteLLM       (localhost:4000 — OpenAI-compatible API)
    ↓
llama-swap    (localhost:8080 — hot-swaps models on demand)
    ↓
llama-server  (localhost:8081 — llama.cpp HTTP server)
    ↓
GGUF model    (loaded from disk into VRAM)
```

Cloud providers (Anthropic, OpenAI, Gemini) remain in the fallback chain. If the local stack is unavailable, Forge fails over to cloud automatically.

### Models and roles

| Role | Default alias | Purpose |
|---|---|---|
| `coding` | `qwen3` | Code generation, refactoring, documentation |
| `reasoning` | `deepseek` | Architecture, planning, mathematics, multi-step reasoning |
| `vision` | `qwen3vl` | Image understanding, visual Q&A |

Forge routes automatically: `forge run "..." --type coding` uses `qwen3`, `--type analysis` or `--type planning` uses `deepseek`. You never specify a model name.

### Setup

**1. Prerequisites**

Install these once:
```bash
pip install litellm
```

Have these binaries available (you said you already have them):
- `llama-server.exe` — llama.cpp HTTP server
- `llama-swap.exe` — hot-swap proxy

**2. Configure `.env`**

```bash
FORGE_LOCAL_STACK_ENABLED=true

# Paths to binaries
FORGE_LLAMA_CPP_BINARY=C:\path\to\llama-server.exe
FORGE_LLAMA_SWAP_BINARY=C:\path\to\llama-swap.exe

# Directories containing your .gguf files (semicolon-separated on Windows)
FORGE_MODELS_DIR=D:\models;E:\models-IMP

# GPU layers (99 = all layers on GPU — reduce if you hit VRAM limits)
FORGE_GPU_LAYERS=99
FORGE_THREADS=8
```

**3. Start the stack**

```bash
forge stack start
```

Forge will:
- Scan `FORGE_MODELS_DIR` for `.gguf` files
- Auto-detect roles from filenames (`*Qwen3*` → coding, `*DeepSeek*` → reasoning, `*VL*` → vision)
- Associate `mmproj-*.gguf` files with their vision model
- Generate `llama_swap_config.yaml` and `litellm_config.yaml`
- Start llama-swap and LiteLLM as background processes
- Wait for both to become healthy before returning

**4. Run tasks**

```bash
# Uses qwen3 (coding model) — no configuration needed
forge run "Implement a binary search tree with insert, delete, and search"

# Uses deepseek (reasoning model)
forge run "Design the database schema for a multi-tenant SaaS" --type planning

# Still works when stack is running — forge health shows both
forge health
```

### Model discovery

Forge scans `FORGE_MODELS_DIR` recursively for `*.gguf` files and detects roles from filenames:

| Filename pattern | Detected role |
|---|---|
| `*mmproj*` | vision projection (attached to vision model, not standalone) |
| `*VL*`, `*vl*`, `*vision*` | vision |
| `*DeepSeek*`, `*deepseek*`, `*R1*`, `*reasoning*` | reasoning |
| `*Qwen*`, `*qwen*`, `*coding*`, `*CodeLlama*` | coding |
| anything else | custom |

Your specific files at `D:\models` and `E:\models-IMP`:

| File | Detected role | Alias |
|---|---|---|
| `coding-Qwen3-8B-Q4_K_M.gguf` | coding | qwen3 |
| `language-Qwen3VL-8B-Instruct-Q4_K_M.gguf` | vision | qwen3vl |
| `vision-mmproj-Qwen3VL-8B-Instruct-F16.gguf` | mmproj | — (attached to vision) |
| `reasoning-DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf` | reasoning | deepseek |

To override auto-detection for any model:
```bash
FORGE_CODING_MODEL_PATH=D:\models\coding-Qwen3-8B-Q4_K_M.gguf
FORGE_REASONING_MODEL_PATH=E:\models-IMP\reasoning-DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf
FORGE_VISION_MODEL_PATH=D:\models\language-Qwen3VL-8B-Instruct-Q4_K_M.gguf
FORGE_MMPROJ_PATH=D:\models\vision-mmproj-Qwen3VL-8B-Instruct-F16.gguf
```

### OpenAI-compatible endpoint

When the stack is running, `localhost:4000` is a fully OpenAI-compatible API endpoint. Use it with any tool that supports OpenAI:

```bash
# Continue (VS Code extension)
"openAIServerUrl": "http://localhost:4000"

# Aider
aider --openai-api-base http://localhost:4000 --model qwen3

# curl
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Configuration

```bash
# Stack
FORGE_LOCAL_STACK_ENABLED=false    # true to activate
FORGE_LLAMA_CPP_BINARY=            # path to llama-server binary
FORGE_LLAMA_SWAP_BINARY=           # path to llama-swap binary
FORGE_LLAMA_SERVER_PORT=8081       # llama.cpp port (managed by llama-swap)
FORGE_LLAMA_SWAP_PORT=8080         # llama-swap port
FORGE_LITELLM_PORT=4000            # LiteLLM OpenAI-compatible port
FORGE_LITELLM_URL=http://localhost:4000

# Models
FORGE_MODELS_DIR=D:\models;E:\models-IMP   # semicolon-separated on Windows
FORGE_CODING_MODEL=qwen3                    # alias passed to llama-swap
FORGE_REASONING_MODEL=deepseek
FORGE_VISION_MODEL=qwen3vl

# Hardware
FORGE_GPU_LAYERS=99    # reduce if VRAM insufficient (try 40, 20, 0)
FORGE_THREADS=8        # CPU threads for prompt processing
```

### New files

```
forge/
├── local_stack/
│   ├── __init__.py
│   ├── model_registry.py    # GGUF discovery, role detection, mmproj association
│   ├── config_generator.py  # auto-generates llama-swap + LiteLLM YAML
│   ├── health.py            # liveness checks for each stack component
│   └── launcher.py          # spawns + monitors llama-swap and LiteLLM
├── providers/base.py        # + LiteLLMProvider (OpenAI-compat, zero cost)
├── config.py                # + ModelProfile, LocalStackConfig, RouterConfig.reasoning_provider
└── router/model_router.py   # + task-type → alias routing for litellm provider
```

---

## Known Behaviours

**Task interpretation** — Forge treats all inputs as coding tasks by default. "Explain this repo" produces a script that explains repos. Use `--type analysis` to trigger the RESEARCH phase for research/explanation tasks.

**Tokens Used: 0 with Ollama** — Ollama does not return token counts in its API response. Cloud providers return accurate counts.

**All providers loading at startup** — All configured providers load even with blank keys. They fail fast at auth time. Set `FORGE_FALLBACK_CHAIN` to only providers you have keys for.

**Bash tool disabled by default** — Set `FORGE_ALLOW_BASH=true`. Commands run in the workspace directory with the current user's permissions.

**Ctrl-C is safe** — Kills the current LLM call. Task saved as FAILED. Resume with `forge resume TASK-NNN`.

**Performance gate is warn-only** — By default, complexity violations are logged but do not block the pipeline. Set `FORGE_COMPLEXITY_GATE=fail` to enforce hard gates.

**Security gate blocks on CRITICAL** — OWASP CRITICAL findings (SQL injection, `eval()`, unsafe deserialization, private keys) fail the REVIEW gate. The Supervisor classifies this as a deterministic rejection and does not retry — retrying the same code against the same agent would produce the same result.

**BLOCKED vs FAILED** — A task becomes `FAILED` when a deterministic gate rejects it (bad code, security violation, test failure that can't be fixed). A task becomes `BLOCKED` when all retry budgets are exhausted on transient failures (provider down, network error). BLOCKED tasks can be resumed once the infrastructure issue is resolved.

**Workspace output directory** — Default is `workspace/` in the current directory. Override per-run with `forge run --output /path/to/dir` or permanently with `FORGE_WORKSPACE=/path/to/dir`. The path is resolved to absolute at config load time and honoured by the engine, tool calls (`write_file`, `list_files`, `git_clone`), and file integrations. Changing `FORGE_WORKSPACE` mid-process is safe — all three subsystems read from live config, not a frozen module constant.

**Repository Intelligence AS-IS prompt** — When `forge run --repo ./path` is used, the Repository Intelligence Engine runs before the RESEARCH phase. It produces an AS-IS prompt describing the codebase architecture and injects it into the ResearchAgent's context. This does not require an LLM call — it is pure static analysis. If the repo path is invalid or unreadable, a warning is injected and the task continues without it.

**Repository comparison** — `forge intelligence repo compare` analyses both repos independently and produces a diff. It does not use git diff — it compares structural fingerprints (packages, classes, patterns, tech stack) so it works even across language versions or after significant refactoring.

**Test Intelligence is always on** — The TestIntelligenceEngine runs after every EXECUTE phase as a non-fatal hook. If the implementation is not Python (e.g. a bash script or SQL), the AST parse will fail silently and the QA agent will proceed without the brief. Set `FORGE_TI_ENABLED=false` to disable entirely.

**`forge intelligence brief` vs `forge intelligence repo brief`** — `brief` (no `repo`) analyses a *single Python file* and prints the QA brief for that file's content. `repo brief` analyses an *entire directory* and prints the AS-IS prompt for the whole repository — these are two different outputs for two different purposes.

**Privacy ledger is in-process memory** — The redaction ledger (token→original map) lives in memory within the running process. It is not persisted to disk. Entries expire after `FORGE_PRIVACY_LEDGER_TTL_S` seconds (default 300). If the process restarts mid-call, the response will be scrubbed via stateless fallback scan.

**Meta-learning is automatic** — `LearningEngine` runs after every task completion. On very fast machines this adds ~100ms. Set `FORGE_AUTO_LEARN=false` to disable.

**Forge Thunderbird — first model request loads the model** — llama-swap starts llama-server lazily on the first request. The first `forge run` after `forge stack start` will be slow (30s–3min depending on model size and disk speed) while the GGUF loads into VRAM. Subsequent requests to the same model are fast. Switching models (e.g. from `qwen3` to `deepseek`) triggers a reload.

**Forge Thunderbird — VRAM limits** — if `FORGE_GPU_LAYERS=99` causes an out-of-memory error in llama-server, reduce it. A 14B Q4 model needs ~8GB VRAM fully on GPU. Use `FORGE_GPU_LAYERS=0` for CPU-only inference (much slower but no VRAM limit). Mixed CPU/GPU offloading (e.g. `FORGE_GPU_LAYERS=20`) splits layers between RAM and VRAM.

**Forge Thunderbird — llama-swap manages llama-server** — never start llama-server manually when using `forge stack start`. llama-swap owns that process and will kill and restart it on model switches. Running a manual llama-server on port 8081 will cause bind failures.

**Forge Thunderbird — config files are regenerated on each `forge stack start`** — if you manually edited `llama_swap_config.yaml` or `litellm_config.yaml`, set `FORGE_AUTO_GENERATE_CONFIGS=false` in `.env` to preserve your edits.

---

## Roadmap

The following capabilities from the FORGE Roadmap document are planned for future implementation:

**Next priorities (in order):**
1. ⚠️ **Web UI rework** — Full Gradio 6.x rewrite with streaming, live pipeline bar, memory panel
2. 📋 **Billing & metering** — Usage accounting ledger, provider cost normalisation, billing feature flags
3. 🔍 **Type safety validation** — AST + mypy integration, type mismatch detection
4. 📐 **Architecture compliance** — Import layer enforcement, circular dependency detection
5. 📝 **Docstring completeness** — Format validation, parameter/return doc checking
6. 🧪 **Mutation testing** — Test coverage verification via code mutation
7. 🔄 **Deterministic replay** — Re-execute any task from checkpoint with same LLM responses
8. 🏗️ **Domain-specific validators** — Django, FastAPI, Kubernetes, Terraform plugins
9. ⚡ **Parallel task execution** — Multiple agents on same codebase with conflict resolution
10. 🔗 **Cross-task dependency management** — DAG-based task ordering and cascade handling

---

## Live Test Results (Ollama / gpt-oss:20b / 24GB RAM)

| Phase | Time | Notes |
|-------|------|-------|
| PLAN | ~10 min | Includes cold model load |
| EXECUTE | ~19 min | + immediate AST validation (< 1s) |
| VERIFY | ~22 min | After context trimming (FORGE_CONTEXT_CHARS=4000) |
| REVIEW + TEST | ~17 min | Parallel + full OWASP scan |
| COMMIT | ~17 min | |
| **Total** | **~85 min** | Post-tuning, no timeouts |

Key tuning: `FORGE_CONTEXT_CHARS=4000`, `FORGE_MAX_RETRIES=1`, `max_tokens=512` for Reviewer/QA.