"""
FORGE Live Test with Claude Inline Provider
============================================
FORGE orchestrates the full pipeline. Every LLM call is intercepted,
the prompt is written to a file, Claude reads it and writes the response,
then FORGE continues. Full traceability, real FORGE pipeline.
"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, '/home/claude/forge')
os.environ['_DUMMY'] = 'inline'

from forge.config import ForgeConfig
from forge.providers.base import BaseProvider, CompletionRequest, CompletionResponse
from forge.orchestration.engine import OrchestrationEngine
from forge.orchestration.task import TaskType
from forge.router.model_router import ModelRouter
from forge.storage.task_repository import TaskRepository
from forge.telemetry.logger import setup_logging

setup_logging('ERROR')

WORK_DIR = Path('/home/claude/forge/workspace/live_run')
WORK_DIR.mkdir(parents=True, exist_ok=True)

call_log = []

class ClaudeInlineProvider(BaseProvider):
    """
    Provider that writes prompts to files and reads responses.
    Claude (via tool calls) reads the prompt files and writes responses.
    """
    name = 'anthropic'
    default_model = 'claude-opus-4-5'

    def __init__(self):
        super().__init__(api_key='inline', base_url='inline')
        self.call_count = 0

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.call_count += 1
        call_id = f"call_{self.call_count:03d}"
        t0 = time.monotonic()

        # Write prompt
        prompt_file = WORK_DIR / f"{call_id}_prompt.json"
        prompt_data = {
            'call_id': call_id,
            'model': 'claude-opus-4-5',
            'system': request.system_prompt,
            'messages': [{'role': m.role, 'content': m.content} for m in request.messages],
            'max_tokens': request.max_tokens,
            'task_id': request.task_id,
        }
        prompt_file.write_text(json.dumps(prompt_data, indent=2))

        # Wait for response file
        response_file = WORK_DIR / f"{call_id}_response.txt"
        print(f"\n{'='*60}", flush=True)
        print(f"FORGE needs LLM call #{self.call_count}", flush=True)
        print(f"Prompt written to: {prompt_file}", flush=True)
        print(f"Waiting for response at: {response_file}", flush=True)
        print(f"(Claude will respond via tool call)", flush=True)

        # Poll for response
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if response_file.exists():
                content = response_file.read_text().strip()
                elapsed = time.monotonic() - t0
                call_log.append({
                    'call_id': call_id,
                    'elapsed_s': round(elapsed, 1),
                    'tokens_approx': len(content.split()) * 4 // 3,
                })
                print(f"Response received ({len(content)} chars, {elapsed:.1f}s)", flush=True)
                return CompletionResponse(
                    content=content, model='claude-opus-4-5', provider='anthropic',
                    tokens_input=len(json.dumps(prompt_data))//4,
                    tokens_output=len(content)//4, duration_ms=int(elapsed*1000)
                )
            time.sleep(0.5)

        raise Exception(f"Timeout waiting for response to {call_id}")

    def health_check(self): return True


# Setup
cfg = ForgeConfig()
cfg.task_dir = WORK_DIR / 'TASKS'
cfg.log_dir = WORK_DIR / 'logs'
cfg.workspace_dir = WORK_DIR
cfg.ensure_dirs()
os.environ['_DUMMY'] = 'inline-key'
cfg.providers['anthropic'].api_key_env = '_DUMMY'
cfg.router.coding_provider = 'anthropic'
cfg.router.review_provider = 'anthropic'
cfg.router.large_context_provider = 'anthropic'
cfg.router.default_provider = 'anthropic'
cfg.router.fallback_chain = ['anthropic']

provider = ClaudeInlineProvider()
repo = TaskRepository(db_path=str(WORK_DIR/'forge.db'), task_dir=cfg.task_dir)

# Build router with our inline provider
router = ModelRouter.__new__(ModelRouter)
router.config = cfg
router._providers = {'anthropic': provider}

phase_times = {}
engine = OrchestrationEngine(config=cfg, repo=repo, router=router)
orig = engine._execute_phase
def timed(phase, ctx):
    t0 = time.monotonic()
    print(f"\n>>> PHASE {phase.value} START", flush=True)
    result = orig(phase, ctx)
    elapsed = round(time.monotonic()-t0, 1)
    phase_times[phase.value] = elapsed
    print(f"<<< PHASE {phase.value} {'PASS' if result[0] else 'FAIL'} ({elapsed}s)", flush=True)
    return result
engine._execute_phase = timed

# Create task
task = engine.create_task(
    title='Morning Workflow Dashboard',
    description="""Build a complete Streamlit dashboard (single file: dashboard.py) that solves the problem of context-switching between multiple tools every morning.

The dashboard aggregates:
1. Daily tasks with priority, status, due date — stored in tasks.json, persists between sessions
2. RSS/content feeds from 2 configurable sources (default: Hacker News, ArXiv CS)
3. AI-generated content ideas — 5-8 ideas derived from today's feed headlines
4. Morning briefing at top of Today page: tasks due today, total feed items, top idea of the day

Pages (sidebar navigation):
- Today: morning briefing + task list (add, complete, delete tasks)
- Feeds: all feed items sorted by date with source badge
- Ideas: content idea cards with tags
- Settings: configure feed URLs, items per feed, app title

Technical constraints:
- Single file: dashboard.py
- Dependencies: streamlit, requests, feedparser, datetime (stdlib)
- tasks.json for task persistence
- All state in st.session_state
- Handle feed fetch errors gracefully (show warning, continue)
- Clean, professional UI using Streamlit native components only""",
    task_type=TaskType.CODING,
    acceptance_criteria=[
        'streamlit run dashboard.py starts without import errors',
        'Tasks load from and save to tasks.json',
        'RSS feeds from 2+ sources display in Feeds page',
        'Morning briefing shows correct counts on Today page',
        'All 4 sidebar pages render without errors',
        'Settings page persists feed configuration to settings.json',
        'Content ideas section shows 5+ ideas',
        'Feed errors handled gracefully with st.warning',
    ],
    priority=1,
)
print(f"\nTASK-{task.sequence_number:03d} created (ID: {task.id})", flush=True)
print(f"State: {task.state.value}", flush=True)
(WORK_DIR/'task_id.txt').write_text(task.id)

t_total = time.monotonic()
result = engine.run_task(task.id)
total_s = round(time.monotonic()-t_total, 1)

# Save results
results = {
    'task_id': task.id, 'sequence': task.sequence_number,
    'success': result.success, 'total_seconds': total_s,
    'phase_times': phase_times, 'llm_calls': call_log,
    'total_calls': len(call_log), 'error': result.error,
}
(WORK_DIR/'results.json').write_text(json.dumps(results, indent=2))
print(f"\n{'='*60}", flush=True)
print(f"FORGE RESULT: {'SUCCESS' if result.success else 'FAILED'}", flush=True)
print(f"Total: {total_s}s | Calls: {len(call_log)}", flush=True)
print(json.dumps(results, indent=2), flush=True)
