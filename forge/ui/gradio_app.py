"""
FORGE Gradio UI
===============
Chat-first interface for the FORGE AI engineering platform.
Run: python -m forge.ui.gradio_app
     (or: python forge/ui/gradio_app.py)

Requires: pip install gradio
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# ── Ensure forge package is importable when run directly ─────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.config import ForgeConfig, get_config
from forge.orchestration.task import TaskState, TaskType
from forge.telemetry.logger import setup_logging

try:
    import gradio as gr
except ImportError:
    print("Gradio not installed. Run: pip install gradio")
    sys.exit(1)

import gradio_client.utils as gutils

orig = gutils._json_schema_to_python_type

#def debug_schema(schema, defs=None):
#    try:
#        return orig(schema, defs)
#    except Exception:
#        print("\n=== FAILING SCHEMA ===")
#        print(repr(schema))
#        raise
#
#gutils._json_schema_to_python_type = debug_schema
# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & THEME
# ─────────────────────────────────────────────────────────────────────────────

FORGE_VERSION = "1.0.0"

PROVIDER_MODELS: dict[str, list[str]] = {
    "anthropic":  [
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ],
    "openai":     ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3"],
    "gemini":     ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-pro"],
    "ollama":     ["gpt-oss:20b", "llama3.2", "llama3.1", "mistral", "codellama", "deepseek-coder", "phi3"],
    "openrouter": [
        "anthropic/claude-opus-4-5",
        "openai/gpt-4o",
        "google/gemini-pro-1.5",
        "meta-llama/llama-3.1-70b-instruct",
        "mistralai/mistral-large",
    ],
}

TASK_TYPE_LABELS: dict[str, str] = {
    "coding":        "💻 Coding",
    "review":        "🔍 Review",
    "testing":       "🧪 Testing",
    "security":      "🔒 Security",
    "documentation": "📝 Documentation",
    "planning":      "🗺️ Planning",
    "analysis":      "📊 Analysis (+ Research)",
}

PIPELINE_PHASES = ["PLAN", "EXECUTE", "VERIFY", "REVIEW", "TEST", "COMMIT"]

STATE_EMOJI = {
    "NEW": "⬜", "READY": "🔵", "IN_PROGRESS": "🟡",
    "BLOCKED": "🟠", "FAILED": "🔴", "VERIFIED": "🔵", "COMPLETE": "✅",
}


# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM CSS  —  feel free to modify any variable
# ─────────────────────────────────────────────────────────────────────────────

FORGE_CSS = """
/* ── Variables — change these to retheme ──────────────────────── */
:root {
  --forge-bg:           #0f1117;
  --forge-surface:      #1a1d27;
  --forge-surface-2:    #21253a;
  --forge-border:       #2d3352;
  --forge-accent:       #6c63ff;
  --forge-accent-glow:  rgba(108,99,255,0.25);
  --forge-success:      #22c55e;
  --forge-warning:      #f59e0b;
  --forge-error:        #ef4444;
  --forge-text:         #e2e8f0;
  --forge-text-dim:     #7c8db5;
  --forge-font:         'Inter', system-ui, sans-serif;
  --forge-mono:         'JetBrains Mono', 'Fira Code', monospace;
  --forge-radius:       10px;
  --forge-radius-lg:    16px;
}

/* ── Base ─────────────────────────────────────────────────────── */
body, .gradio-container {
  background: var(--forge-bg) !important;
  font-family: var(--forge-font) !important;
  color: var(--forge-text) !important;
}

/* ── Header ───────────────────────────────────────────────────── */
#forge-header {
  background: linear-gradient(135deg, #1a1d27 0%, #0f1117 100%);
  border-bottom: 1px solid var(--forge-border);
  padding: 18px 28px;
  display: flex;
  align-items: center;
  gap: 14px;
  border-radius: var(--forge-radius-lg) var(--forge-radius-lg) 0 0;
}
#forge-logo {
  font-size: 24px;
  font-weight: 800;
  letter-spacing: -0.5px;
  background: linear-gradient(135deg, #6c63ff, #a78bfa);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
#forge-tagline {
  font-size: 12px;
  color: var(--forge-text-dim);
  margin-top: 2px;
}
#forge-version-badge {
  margin-left: auto;
  font-size: 11px;
  padding: 3px 10px;
  background: var(--forge-surface-2);
  border: 1px solid var(--forge-border);
  border-radius: 20px;
  color: var(--forge-text-dim);
}

/* ── Panels ───────────────────────────────────────────────────── */
.forge-panel {
  background: var(--forge-surface) !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: var(--forge-radius) !important;
  padding: 16px !important;
}
.forge-panel-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--forge-text-dim);
  margin-bottom: 12px;
}

/* ── Chat window ──────────────────────────────────────────────── */
#chat-window {
  background: var(--forge-surface) !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: var(--forge-radius) !important;
  min-height: 460px !important;
}
#chat-window .message {
  border-radius: 8px !important;
  margin: 6px 0 !important;
}
#chat-window .message.user {
  background: var(--forge-surface-2) !important;
  border: 1px solid var(--forge-border) !important;
}
#chat-window .message.bot {
  background: linear-gradient(135deg, #1e1b3a, #1a1d27) !important;
  border: 1px solid var(--forge-accent) !important;
  border-left: 3px solid var(--forge-accent) !important;
}

/* ── Input row ────────────────────────────────────────────────── */
#prompt-input textarea {
  background: var(--forge-surface-2) !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: var(--forge-radius) !important;
  color: var(--forge-text) !important;
  font-family: var(--forge-font) !important;
  font-size: 14px !important;
  resize: vertical !important;
  transition: border-color 0.2s !important;
}
#prompt-input textarea:focus {
  border-color: var(--forge-accent) !important;
  box-shadow: 0 0 0 3px var(--forge-accent-glow) !important;
}

/* ── Buttons ──────────────────────────────────────────────────── */
#run-btn {
  background: linear-gradient(135deg, #6c63ff, #8b5cf6) !important;
  border: none !important;
  border-radius: var(--forge-radius) !important;
  color: white !important;
  font-weight: 600 !important;
  font-size: 14px !important;
  padding: 12px 20px !important;
  cursor: pointer !important;
  transition: all 0.2s !important;
  box-shadow: 0 4px 15px var(--forge-accent-glow) !important;
}
#run-btn:hover {
  transform: translateY(-1px) !important;
  box-shadow: 0 6px 20px var(--forge-accent-glow) !important;
}
#run-btn:active {
  transform: translateY(0) !important;
}
#clear-btn {
  background: var(--forge-surface-2) !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: var(--forge-radius) !important;
  color: var(--forge-text-dim) !important;
  font-size: 13px !important;
  padding: 10px 16px !important;
  transition: all 0.2s !important;
}
#clear-btn:hover {
  border-color: var(--forge-text-dim) !important;
  color: var(--forge-text) !important;
}

/* ── Dropdowns / Selects ──────────────────────────────────────── */
.forge-select select, .forge-select input {
  background: var(--forge-surface-2) !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: 8px !important;
  color: var(--forge-text) !important;
  font-size: 13px !important;
}

/* ── Pipeline status bar ──────────────────────────────────────── */
#pipeline-bar {
  display: flex;
  gap: 4px;
  align-items: center;
  margin: 8px 0;
}
.phase-chip {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 20px;
  border: 1px solid var(--forge-border);
  background: var(--forge-surface-2);
  color: var(--forge-text-dim);
  transition: all 0.3s;
}
.phase-chip.active {
  background: var(--forge-accent) !important;
  border-color: var(--forge-accent) !important;
  color: white !important;
  box-shadow: 0 0 10px var(--forge-accent-glow);
}
.phase-chip.done {
  background: #14532d !important;
  border-color: var(--forge-success) !important;
  color: var(--forge-success) !important;
}
.phase-chip.failed {
  background: #450a0a !important;
  border-color: var(--forge-error) !important;
  color: var(--forge-error) !important;
}
.phase-arrow {
  color: var(--forge-text-dim);
  font-size: 12px;
}

/* ── Metrics row ──────────────────────────────────────────────── */
.forge-metric {
  background: var(--forge-surface-2);
  border: 1px solid var(--forge-border);
  border-radius: 8px;
  padding: 10px 14px;
  text-align: center;
}
.forge-metric-val {
  font-size: 20px;
  font-weight: 700;
  color: var(--forge-text);
}
.forge-metric-label {
  font-size: 10px;
  color: var(--forge-text-dim);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-top: 2px;
}

/* ── Sidebar sections ─────────────────────────────────────────── */
.sidebar-section {
  margin-bottom: 20px;
}
.sidebar-section-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--forge-text-dim);
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--forge-border);
}

/* ── History placeholder ──────────────────────────────────────── */
#history-placeholder {
  background: var(--forge-surface-2);
  border: 1px dashed var(--forge-border);
  border-radius: var(--forge-radius);
  padding: 20px 16px;
  text-align: center;
  color: var(--forge-text-dim);
  font-size: 12px;
}
#history-placeholder .ph-icon {
  font-size: 28px;
  margin-bottom: 8px;
}
#history-placeholder .ph-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--forge-text);
  margin-bottom: 4px;
}

/* ── Code blocks in chat ──────────────────────────────────────── */
#chat-window code {
  font-family: var(--forge-mono) !important;
  background: #0d1117 !important;
  padding: 2px 6px !important;
  border-radius: 4px !important;
  font-size: 12px !important;
}
#chat-window pre {
  background: #0d1117 !important;
  border: 1px solid var(--forge-border) !important;
  border-radius: 8px !important;
  padding: 12px !important;
  overflow-x: auto !important;
}

/* ── Scrollbars ───────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--forge-surface); }
::-webkit-scrollbar-thumb { background: var(--forge-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--forge-text-dim); }

/* ── Misc ─────────────────────────────────────────────────────── */
label { color: var(--forge-text-dim) !important; font-size: 12px !important; }
.gr-form { background: transparent !important; }
footer { display: none !important; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# FORGE ENGINE  (lazy-loaded)
# ─────────────────────────────────────────────────────────────────────────────

_engine = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from forge.orchestration.engine import OrchestrationEngine
                _engine = OrchestrationEngine()
    return _engine


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE RUNNER  (streaming)
# ─────────────────────────────────────────────────────────────────────────────

def _pipeline_status_html(phases_done: list[str], phase_active: str | None) -> str:
    """Render the PLAN→EXECUTE→…→COMMIT status bar as HTML."""
    chips = []
    for p in PIPELINE_PHASES:
        if p in phases_done:
            cls = "done"
        elif p == phase_active:
            cls = "active"
        else:
            cls = ""
        chips.append(f'<span class="phase-chip {cls}">{p}</span>')
        if p != PIPELINE_PHASES[-1]:
            chips.append('<span class="phase-arrow">›</span>')
    return f'<div id="pipeline-bar">{"".join(chips)}</div>'


def run_forge_streaming(
    prompt,
    task_type,
    provider,
    model,
    priority,
    criteria_raw,
    history,
):
    """
    Run the FORGE pipeline and yield (history, pipeline_html, status_text) tuples
    so the UI updates incrementally as each phase completes.
    """
    if not prompt.strip():
        yield history, _pipeline_status_html([], None), "⚠ Enter a task description."
        return

    # ── Apply provider/model to config ───────────────────────────────────────
    cfg = get_config()
    cfg.router.default_provider = provider
    cfg.router.coding_provider  = provider
    cfg.router.review_provider  = provider
    cfg.router.fallback_chain   = [provider]

    # Set model override for the chosen provider
    model_attr = f"{provider}_model"
    if hasattr(cfg.router, model_attr):
        setattr(cfg.router, model_attr, model)

    # ── Parse acceptance criteria ─────────────────────────────────────────────
    criteria = [c.strip() for c in criteria_raw.split("\n") if c.strip()] if criteria_raw else []

    # ── User message bubble ───────────────────────────────────────────────────
    ts = datetime.now().strftime("%H:%M")
    user_msg = (
        f"**Task** `{task_type}` · **Provider** `{provider}/{model}` · "
        f"**Priority** `{priority}` · `{ts}`\n\n{prompt}"
    )
    if criteria:
        user_msg += "\n\n**Acceptance criteria:**\n" + "\n".join(f"- {c}" for c in criteria)

    #1 history = history + [[user_msg, None]]
    history = history + [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": ""}
    ]
    yield history, _pipeline_status_html([], None), "🚀 Starting FORGE pipeline…"

    # ── Instrument engine ─────────────────────────────────────────────────────
    engine = get_engine()
    phases_done: list[str] = []
    phase_results_detail: dict[str, str] = {}
    phase_times: dict[str, float] = {}

    # Wrap _execute_phase to get live phase updates
    orig_execute = engine._execute_phase

    def instrumented_phase(phase, ctx):
        t0 = time.monotonic()
        result = orig_execute(phase, ctx)
        elapsed = round(time.monotonic() - t0, 1)
        phase_times[phase.value] = elapsed
        return result

    engine._execute_phase = instrumented_phase

    # ── Create task ───────────────────────────────────────────────────────────
    try:
        task_type_enum = TaskType(task_type)
    except ValueError:
        task_type_enum = TaskType.CODING

    task = engine.create_task(
        title=prompt[:80] + ("…" if len(prompt) > 80 else ""),
        description=prompt,
        task_type=task_type_enum,
        acceptance_criteria=criteria,
        priority=priority,
    )

    task_ref = f"TASK-{task.sequence_number:03d} `{task.id}`"
    bot_progress = f"### ⚙️ {task_ref}\n\nRunning FORGE pipeline with `{provider}/{model}`…\n\n"
    print("HISTORY =", repr(history))
    #2 history[-1][1] = bot_progress
    history[-1]["content"] = bot_progress
    yield history, _pipeline_status_html([], "PLAN"), f"▶ PLAN phase — Planner agent…"

    # ── Run pipeline in thread so we can yield progress ───────────────────────
    result_holder: dict = {}
    exception_holder: dict = {}

    def _run():
        try:
            result_holder["result"] = engine.run_task(task.id)
        except Exception as e:
            exception_holder["error"] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Poll task state and phase_times while pipeline runs
    last_phase_count = 0
    poll_interval = 1.5
    start_wall = time.monotonic()

    while t.is_alive():
        time.sleep(poll_interval)
        elapsed_total = round(time.monotonic() - start_wall, 0)

        # Infer which phases completed from phase_times dict
        completed = list(phase_times.keys())
        active_idx = len(completed)
        active_phase = PIPELINE_PHASES[active_idx] if active_idx < len(PIPELINE_PHASES) else None

        # Build progress message
        progress_lines = [f"### ⚙️ {task_ref}", ""]
        for p in PIPELINE_PHASES:
            if p in completed:
                t_val = phase_times.get(p, 0)
                progress_lines.append(f"✅ **{p}** — {t_val}s")
            elif p == active_phase:
                progress_lines.append(f"🔄 **{p}** — running…")
            else:
                progress_lines.append(f"⬜ {p}")

        progress_lines += ["", f"*Elapsed: {elapsed_total:.0f}s*"]
        print("HISTORY =", repr(history))
        #3 history[-1][1] = "\n".join(progress_lines)
        history[-1]["content"] = "\n".join(progress_lines)
        status = f"▶ {active_phase or 'COMMIT'} phase running… ({elapsed_total:.0f}s elapsed)"
        yield history, _pipeline_status_html(completed, active_phase), status

    t.join()

    # ── Restore engine ────────────────────────────────────────────────────────
    engine._execute_phase = orig_execute

    # ── Handle exception ──────────────────────────────────────────────────────
    if exception_holder:
        err = exception_holder["error"]
        print("HISTORY =", repr(history))
        #4 history[-1][1] 
        history[-1]["content"] = (
            f"### ❌ Pipeline error\n\n```\n{err}\n```\n\n"
            f"Check your API key for `{provider}` in `.env`"
        )
        yield history, _pipeline_status_html([], None), f"❌ Error: {err[:80]}"
        return

    result = result_holder.get("result")
    if not result:
        print("HISTORY =", repr(history))
        #5 history[-1][1]
        history[-1]["content"] = "### ❌ No result returned"
        yield history, _pipeline_status_html([], None), "❌ No result"
        return

    # ── Format final response ─────────────────────────────────────────────────
    completed_phases = list(phase_times.keys())
    total_s = sum(phase_times.values())

    if result.success:
        # Build rich success response
        lines = [
            f"### ✅ {task_ref} — COMPLETE",
            "",
            f"**Provider:** `{result.provider_used or provider}` · "
            f"**Tokens:** `{result.tokens_used:,}` · "
            f"**Time:** `{total_s:.1f}s`",
            "",
            "#### Pipeline results",
        ]
        for p in PIPELINE_PHASES:
            t_val = phase_times.get(p)
            if t_val is not None:
                lines.append(f"- ✅ **{p}** — {t_val}s")

        # Show review findings if any
        if isinstance(result.output, dict):
            review = result.output.get("review", {})
            security = result.output.get("security", {})
            if isinstance(review, dict) and review.get("issues"):
                issues = review["issues"]
                lines += ["", "#### 🔍 Reviewer findings"]
                for iss in issues[:5]:
                    sev = iss.get("severity", "")
                    desc = iss.get("description", "")[:120]
                    lines.append(f"- `{sev}` — {desc}")

            # Show implementation excerpt
            impl = result.output.get("implementation", "")
            if impl:
                # Find first code block
                import re
                code_match = re.search(r"```python\n(.*?)```", impl, re.DOTALL)
                if code_match:
                    code_excerpt = code_match.group(1)[:800]
                    if len(code_match.group(1)) > 800:
                        code_excerpt += "\n# … (truncated — see TASK file for full output)"
                    lines += [
                        "",
                        "#### 💻 Generated code (excerpt)",
                        f"```python\n{code_excerpt}\n```",
                    ]

        lines += [
            "",
            f"📄 Full output written to: `TASKS/TASK-{task.sequence_number:03d}.md`",
        ]
        print("HISTORY =", repr(history))
        #6 history[-1][1]
        history[-1]["content"] = "\n".join(lines)
        yield (
            history,
            _pipeline_status_html(completed_phases, None),
            f"✅ Complete — {total_s:.1f}s · {result.tokens_used:,} tokens",
        )

    else:
        err = result.error or "Unknown failure"
        lines = [
            f"### ❌ {task_ref} — FAILED",
            "",
            f"**Error:** {err}",
            "",
            "#### Phases completed before failure",
        ]
        for p in PIPELINE_PHASES:
            t_val = phase_times.get(p)
            if t_val:
                lines.append(f"- ✅ {p} ({t_val}s)")
            else:
                lines.append(f"- ❌ {p} (failed)")
                break
        lines += ["", f"Use `forge resume {task.id}` to retry from checkpoint."]
        print("HISTORY =", repr(history))
        #7 history[-1][1]
        history[-1]["content"] = "\n".join(lines)
        yield (
            history,
            _pipeline_status_html(completed_phases, None),
            f"❌ Failed: {err[:80]}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_provider_models(provider):
    models = PROVIDER_MODELS.get(provider, ["default"])
    return gr.update(choices=models, value=models[0])


def get_health_html():
    """Quick provider health check for the sidebar."""
    cfg = get_config()
    rows = []
    for name, p_cfg in cfg.providers.items():
        if name == "ollama":
            key_ok = True
            key_label = "local"
        else:
            key_ok = bool(p_cfg.api_key)
            key_label = "✓ key set" if key_ok else "✗ no key"
        dot = "🟢" if key_ok else "🔴"
        rows.append(f"{dot} **{name}** — {key_label}")
    return "\n".join(rows)


def get_memory_md():
    """Memory store stats for the sidebar."""
    try:
        from forge.memory.store import get_memory_store
        stats = get_memory_store().stats()
        total = stats.get("total", 0)
        by_type = stats.get("by_type", {})
        parts = [f"**{total}** memories stored"]
        for t, c in by_type.items():
            parts.append(f"`{t}`: {c}")
        return "  \n".join(parts) if parts else "_No memories yet_"
    except Exception:
        return "_Memory store not initialised_"


def get_recent_tasks_html():
    """Load last 5 tasks from the database for the sidebar."""
    try:
        from forge.storage.task_repository import TaskRepository
        repo = TaskRepository()
        tasks = repo.list_all()[-5:][::-1]
        if not tasks:
            return "_No tasks yet._"
        lines = []
        for t in tasks:
            emoji = STATE_EMOJI.get(t.state.value, "⬜")
            seq = f"TASK-{t.sequence_number:03d}" if t.sequence_number else t.id[:8]
            title = t.title[:40] + ("…" if len(t.title) > 40 else "")
            lines.append(f"{emoji} `{seq}` — {title}")
        return "\n".join(lines)
    except Exception:
        return "_Could not load tasks._"


# ─────────────────────────────────────────────────────────────────────────────
# GRADIO APP
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> gr.Blocks:
    setup_logging("WARNING")

    with gr.Blocks(
        css=FORGE_CSS,
        title="FORGE — AI Engineering Platform",
        theme=gr.themes.Base(),
    ) as app:

        # ── Header ────────────────────────────────────────────────────────────
        gr.HTML(f"""
        <div id="forge-header">
          <div>
            <div id="forge-logo">⚡ FORGE</div>
            <div id="forge-tagline">Production-Grade AI Software Engineering Platform</div>
          </div>
          <div id="forge-version-badge">v{FORGE_VERSION}</div>
        </div>
        """)

        with gr.Row(equal_height=False):

            # ── LEFT SIDEBAR ─────────────────────────────────────────────────
            with gr.Column(scale=1, min_width=260):

                # Provider & Model
                gr.HTML('<div class="sidebar-section-title">Provider &amp; Model</div>')
                with gr.Group(elem_classes=["forge-panel"]):
                    provider_dd = gr.Dropdown(
                        label="Provider",
                        choices=list(PROVIDER_MODELS.keys()),
                        value="ollama",
                        elem_classes=["forge-select"],
                    )
                    model_dd = gr.Dropdown(
                        label="Model",
                        choices=PROVIDER_MODELS["ollama"],
                        value=PROVIDER_MODELS["ollama"][0],
                        elem_classes=["forge-select"],
                    )
                    provider_dd.change(
                        fn=get_provider_models,
                        inputs=provider_dd,
                        outputs=model_dd,
                    )

                # Task config
                gr.HTML('<div class="sidebar-section-title" style="margin-top:16px;">Task Config</div>')
                with gr.Group(elem_classes=["forge-panel"]):
                    task_type_dd = gr.Dropdown(
                        label="Task type",
                        choices=list(TASK_TYPE_LABELS.keys()),
                        value="coding",
                        elem_classes=["forge-select"],
                    )
                    priority_sl = gr.Slider(
                        label="Priority (1 = highest)",
                        minimum=1, maximum=10, step=1, value=5,
                    )
                    criteria_box = gr.Textbox(
                        label="Acceptance criteria (one per line, optional)",
                        placeholder="e.g.\nFunctions have type hints\nAll errors handled\n...",
                        lines=4,
                        elem_classes=["forge-select"],
                    )

                # Provider health
                gr.HTML('<div class="sidebar-section-title" style="margin-top:16px;">Provider Status</div>')
                health_md = gr.Markdown(get_health_html(), elem_classes=["forge-panel"])
                refresh_health_btn = gr.Button(
                    "↻ Refresh", size="sm",
                    elem_classes=["clear-btn"],
                )
                refresh_health_btn.click(fn=get_health_html, outputs=health_md)

                # ── MEMORY STATS ─────────────────────────────────────────────
                gr.HTML('<div class="sidebar-section-title" style="margin-top:16px;">Memory</div>')
                memory_md = gr.Markdown(get_memory_md(), elem_classes=["forge-panel"])
                refresh_memory_btn = gr.Button("↻ Refresh", size="sm", elem_classes=["clear-btn"])
                refresh_memory_btn.click(fn=get_memory_md, outputs=memory_md)

                # ── HISTORY PLACEHOLDER ───────────────────────────────────────
                # TODO: Wire this to a real project/session database.
                #       Each entry should show: project name, last task, timestamp.
                #       Clicking an entry should restore that session's chat history.
                gr.HTML("""
                <div class="sidebar-section-title" style="margin-top:16px;">
                  Chat &amp; Project History
                </div>
                <div id="history-placeholder">
                  <div class="ph-icon">🗂️</div>
                  <div class="ph-title">History coming soon</div>
                  <div>Past sessions and projects will appear here.
                  Each project groups related FORGE tasks together.</div>
                </div>
                <!-- PLACEHOLDER: replace div above with a gr.Dataset or
                     gr.Dataframe bound to a sessions table in the database.
                     Schema: session_id, project_name, last_task_title,
                             last_task_state, updated_at, task_count -->
                """)

            # ── MAIN COLUMN ──────────────────────────────────────────────────
            with gr.Column(scale=3):

                # Pipeline status bar
                pipeline_html = gr.HTML(
                    _pipeline_status_html([], None),
                    label="Pipeline",
                )

                # Chat window
                chatbot = gr.Chatbot(
                    value=[],
                    elem_id="chat-window",
                    label="",
                                        height=480,
                                        show_label=False,
                )

                # Status line
                status_md = gr.Markdown(
                    "_Ready. Describe a coding task and click **Run FORGE**._",
                )

                # Input row
                with gr.Row():
                    prompt_box = gr.Textbox(
                        placeholder=(
                            "Describe your task…\n\n"
                            "Examples:\n"
                            "• Build a FastAPI endpoint that validates email addresses\n"
                            "• Write a Python class for managing a connection pool\n"
                            "• Review this code for security issues: <paste code>"
                        ),
                        lines=4,
                        max_lines=12,
                        show_label=False,
                        elem_id="prompt-input",
                        scale=5,
                    )

                with gr.Row():
                    run_btn = gr.Button(
                        "⚡ Run FORGE",
                        variant="primary",
                        elem_id="run-btn",
                        scale=3,
                    )
                    clear_btn = gr.Button(
                        "🗑 Clear",
                        elem_id="clear-btn",
                        scale=1,
                    )

                # ── Wire up events ────────────────────────────────────────────
                run_inputs = [
                    prompt_box, task_type_dd, provider_dd, model_dd,
                    priority_sl, criteria_box, chatbot,
                ]
                run_outputs = [chatbot, pipeline_html, status_md]

                run_btn.click(
                    fn=run_forge_streaming,
                    inputs=run_inputs,
                    outputs=run_outputs,
                    api_name=False,
                )
                prompt_box.submit(
                    fn=run_forge_streaming,
                    inputs=run_inputs,
                    outputs=run_outputs,
                    api_name=False,
                )
                clear_btn.click(
                    fn=lambda: ([], _pipeline_status_html([], None), "_Cleared._"),
                    outputs=run_outputs,
                )

        # ── Footer ────────────────────────────────────────────────────────────
        gr.HTML("""
        <div style="text-align:center; padding:14px; font-size:11px;
                    color:var(--forge-text-dim); border-top:1px solid var(--forge-border);
                    margin-top:16px;">
          FORGE v{v} · Pipeline: PLAN → EXECUTE → VERIFY → REVIEW → TEST → COMMIT ·
          All outputs written to <code>TASKS/</code>
        </div>
        """.replace("{v}", FORGE_VERSION))

    return app


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    cfg = get_config()
    app = build_app()

    print(f"\n  ⚡ FORGE UI starting…")
    print(f"  Provider:    {cfg.router.default_provider}")
    print(f"  Task dir:    {cfg.task_dir}")
    print(f"  Available:   {cfg.available_providers()}\n")

    app.launch(
        server_name="127.0.0.1",
        server_port=int(os.environ.get("FORGE_UI_PORT", 7860)),
        share=False,
        show_error=True,
        favicon_path=None,
    )


if __name__ == "__main__":
    main()