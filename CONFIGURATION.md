# FORGE Configuration Reference

All configuration is driven by environment variables. Copy `.env.example` to `.env` and edit.

```bash
cp .env.example .env
```

No configuration requires modifying source code.

---

## Provider API Keys

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ANTHROPIC_API_KEY` | string | — | Anthropic Claude API key. Get at https://console.anthropic.com |
| `OPENAI_API_KEY` | string | — | OpenAI API key. Get at https://platform.openai.com |
| `GOOGLE_API_KEY` | string | — | Google Gemini API key. Get at https://aistudio.google.com |
| `OPENROUTER_API_KEY` | string | — | OpenRouter key. Get at https://openrouter.ai |
| `OLLAMA_BASE_URL` | string | `http://localhost:11434` | Ollama server URL. No key required. |

**At least one provider must be configured.** Ollama works with no API key if running locally.

---

## Routing Configuration

Controls which provider handles which task type.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `FORGE_CODING_PROVIDER` | string | `anthropic` | Provider for coding tasks |
| `FORGE_REVIEW_PROVIDER` | string | `openai` | Provider for code review tasks |
| `FORGE_LARGE_CONTEXT_PROVIDER` | string | `gemini` | Provider for large-context / analysis tasks |
| `FORGE_OFFLINE_PROVIDER` | string | `ollama` | Provider for offline operation |
| `FORGE_DEFAULT_PROVIDER` | string | `anthropic` | Fallback when no type-specific provider is set |
| `FORGE_FALLBACK_CHAIN` | comma-list | `anthropic,openai,gemini,ollama` | Ordered failover chain |

**Example — offline-only setup:**
```env
FORGE_CODING_PROVIDER=ollama
FORGE_REVIEW_PROVIDER=ollama
FORGE_FALLBACK_CHAIN=ollama
OLLAMA_BASE_URL=http://localhost:11434
```

**Example — cost-optimised (use GPT mini for most, Claude for complex):**
```env
FORGE_CODING_PROVIDER=openai
FORGE_REVIEW_PROVIDER=anthropic
FORGE_FALLBACK_CHAIN=openai,anthropic,ollama
```

---

## Core Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `FORGE_WORKSPACE` | path | `workspace` | Directory for workspace files |
| `FORGE_TASK_DIR` | path | `TASKS` | Directory where TASK-NNN.md files are written |
| `FORGE_LOG_DIR` | path | `logs` | Directory for log files |
| `FORGE_MAX_AGENTS` | int | `4` | Maximum concurrent agent executions |
| `FORGE_AGENT_TIMEOUT` | int | `300` | Agent execution timeout in seconds |

---

## Persistence

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_URL` | string | `sqlite:///forge_tasks.db` | Database URL. SQLite for local, PostgreSQL for production |
| `REDIS_URL` | string | `redis://localhost:6379` | Redis URL for checkpointing and queues |

**SQLite (default — no setup needed):**
```env
DATABASE_URL=sqlite:///forge_tasks.db
```

**PostgreSQL (production):**
```env
DATABASE_URL=postgresql://forge:password@localhost:5432/forge
```

---

## Telemetry & Logging

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LOG_LEVEL` | string | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | string | `json` | Log format: `json` (structured) or `text` (human-readable) |
| `OTLP_ENDPOINT` | string | `http://localhost:4317` | OpenTelemetry collector endpoint |
| `NO_COLOR` | any | — | Set to any value to disable CLI colour output |

**Log format — JSON (default, for log aggregators):**
```json
{"timestamp": "2025-05-30T10:00:00Z", "correlation_id": "a1b2c3", "task_id": "TASK-001", "agent": "implementer", "event": "Completion success", "severity": "INFO", "duration_ms": 1240, "outcome": "success"}
```

**Log format — text (for local development):**
```env
LOG_FORMAT=text
LOG_LEVEL=DEBUG
```

---

## Security

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VAULT_ADDR` | string | — | HashiCorp Vault address, e.g. `http://vault:8200` |
| `VAULT_TOKEN` | string | — | Vault token (used if `VAULT_ADDR` is set) |

When `VAULT_ADDR` is set, FORGE will attempt to retrieve API keys from Vault instead of environment variables. This is the recommended approach for production deployments.

---

## API Server

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `FORGE_API_HOST` | string | `0.0.0.0` | API server bind address |
| `FORGE_API_PORT` | int | `8080` | API server port |

---

## Docker / Docker Compose

When using `docker-compose.yml`, these additional variables are used:

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | PostgreSQL password (default: `forge`) |
| `GRAFANA_PASSWORD` | Grafana admin password (default: `forge`) |

---

## Complete .env.example

```env
# ── Provider API Keys ─────────────────────────────────────────────
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
OPENROUTER_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

# ── Routing ───────────────────────────────────────────────────────
FORGE_CODING_PROVIDER=anthropic
FORGE_REVIEW_PROVIDER=openai
FORGE_LARGE_CONTEXT_PROVIDER=gemini
FORGE_OFFLINE_PROVIDER=ollama
FORGE_DEFAULT_PROVIDER=anthropic
FORGE_FALLBACK_CHAIN=anthropic,openai,gemini,ollama

# ── Core ──────────────────────────────────────────────────────────
FORGE_WORKSPACE=workspace
FORGE_TASK_DIR=TASKS
FORGE_LOG_DIR=logs
FORGE_MAX_AGENTS=4
FORGE_AGENT_TIMEOUT=300

# ── Persistence ───────────────────────────────────────────────────
DATABASE_URL=sqlite:///forge_tasks.db
REDIS_URL=redis://localhost:6379

# ── Telemetry ─────────────────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FORMAT=json
OTLP_ENDPOINT=http://localhost:4317

# ── Security ──────────────────────────────────────────────────────
# VAULT_ADDR=http://vault:8200
# VAULT_TOKEN=
```

---

## Model Selection

Each provider has a built-in default model. Override with environment variables:

| Variable | Provider | Default | Options |
|----------|----------|---------|---------|
| `FORGE_ANTHROPIC_MODEL` | Anthropic | `claude-opus-4-5` | `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5`, `claude-opus-4-6`, `claude-sonnet-4-6` |
| `FORGE_OPENAI_MODEL` | OpenAI | `gpt-4o` | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o1`, `o3` |
| `FORGE_GEMINI_MODEL` | Gemini | `gemini-1.5-pro` | `gemini-1.5-pro`, `gemini-1.5-flash`, `gemini-2.0-flash`, `gemini-2.0-pro` |
| `FORGE_OLLAMA_MODEL` | Ollama | `llama3.2` | Any model you have pulled locally |
| `FORGE_OPENROUTER_MODEL` | OpenRouter | `anthropic/claude-opus-4-5` | Any model on openrouter.ai |

**Example — use Haiku for cost efficiency:**
```env
FORGE_ANTHROPIC_MODEL=claude-haiku-4-5
FORGE_CODING_PROVIDER=anthropic
```

**Example — use Ollama offline with CodeLlama:**
```env
FORGE_OLLAMA_MODEL=codellama
FORGE_CODING_PROVIDER=ollama
FORGE_REVIEW_PROVIDER=ollama
FORGE_FALLBACK_CHAIN=ollama
```

**Example — use different models per task type:**
```env
FORGE_CODING_PROVIDER=anthropic
FORGE_ANTHROPIC_MODEL=claude-opus-4-5

FORGE_REVIEW_PROVIDER=openai
FORGE_OPENAI_MODEL=gpt-4o

FORGE_LARGE_CONTEXT_PROVIDER=gemini
FORGE_GEMINI_MODEL=gemini-1.5-pro
```

---

## Gradio UI

| Variable | Default | Description |
|----------|---------|-------------|
| `FORGE_UI_PORT` | `7860` | Port for the Gradio web interface |

**Start the UI:**
```bash
python -m forge.ui.gradio_app
# or
python forge/ui/gradio_app.py
```

Opens at: http://localhost:7860

The UI lets you select provider and model per-task from dropdowns — no `.env` changes needed for one-off overrides.
