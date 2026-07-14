"""
FORGE Live Test Runner
======================
Runs the full PLAN->EXECUTE->VERIFY->REVIEW->TEST->COMMIT pipeline
against the bridge server (which proxies to real Claude API via artifact).
Records timing for every phase.
"""
import json, os, sys, time
from pathlib import Path

os.environ['ANTHROPIC_API_KEY'] = 'bridge-key'  # dummy - bridge ignores it

sys.path.insert(0, '/home/claude/forge')

from forge.config import ForgeConfig
from forge.orchestration.engine import OrchestrationEngine
from forge.orchestration.task import TaskType
from forge.providers.base import AnthropicProvider
from forge.router.model_router import ModelRouter
from forge.storage.task_repository import TaskRepository
from forge.telemetry.logger import setup_logging

setup_logging('WARNING')

# ── Configure FORGE to use bridge ────────────────────────────────
cfg = ForgeConfig()
cfg.task_dir   = Path('/home/claude/forge/workspace/TASKS')
cfg.log_dir    = Path('/home/claude/forge/workspace/logs')
cfg.workspace_dir = Path('/home/claude/forge/workspace/dashboard')
cfg.ensure_dirs()

# Point Anthropic provider at bridge server
cfg.providers['anthropic'].api_key_env = '_DUMMY'
os.environ['_DUMMY'] = 'bridge-key'
cfg.providers['anthropic'].base_url = 'http://localhost:9999'

# Use Anthropic for EVERYTHING (we only have the bridge for Anthropic)
cfg.router.coding_provider        = 'anthropic'
cfg.router.review_provider        = 'anthropic'
cfg.router.large_context_provider = 'anthropic'
cfg.router.default_provider       = 'anthropic'
cfg.router.fallback_chain         = ['anthropic']

repo   = TaskRepository(db_path='/home/claude/forge/workspace/forge_live.db', task_dir=cfg.task_dir)
router = ModelRouter(cfg)

# Patch the model to claude-opus-4-5 for all calls
_orig_complete = router.complete
def _complete_with_opus(request, task_type=None, **kw):
    request.model = 'claude-opus-4-5'
    return _orig_complete(request, task_type=task_type or TaskType.CODING, **kw)
router.complete = _complete_with_opus

engine = OrchestrationEngine(config=cfg, repo=repo, router=router)

# ── Create the task ───────────────────────────────────────────────
TASK_DESCRIPTION = """
Build a Streamlit dashboard that solves the daily workflow problem of
context-switching between multiple tools every morning.

The dashboard must aggregate:
1. Daily tasks with priority, status, due date (stored in JSON, persist between sessions)
2. RSS/content feeds from configurable sources (tech news, research papers, etc.)
3. AI-generated content ideas section (based on feed items)
4. A unified morning briefing at the top showing: tasks due today, unread items count, top idea

Navigation: sidebar with Today / Feeds / Ideas / Settings pages.

Technical requirements:
- Single file: dashboard.py (plus tasks.json for persistence)  
- Dependencies: streamlit, requests, feedparser only
- Settings page: configure RSS feed URLs, number of items to show
- Today page: add task form, mark complete, morning briefing summary
- Feeds page: show items from all feeds, sorted by date, with source label
- Ideas page: show 5-10 content ideas generated from recent feed headlines
- All state managed with st.session_state
- Error handling: if a feed fails, show warning and continue
"""

print('='*60)
print('FORGE LIVE TEST — Dashboard Task')
print('Model: claude-opus-4-5 via bridge -> Anthropic API')
print('='*60)
print()

t_start = time.time()

task = engine.create_task(
    title='Morning Workflow Dashboard',
    description=TASK_DESCRIPTION,
    task_type=TaskType.CODING,
    acceptance_criteria=[
        'streamlit run dashboard.py starts without errors',
        'Tasks persist to tasks.json between sessions',
        'RSS feeds load and display items from 2+ sources',
        'Morning briefing shows tasks due today and feed item count',
        'Sidebar navigation works for all 4 pages',
        'Settings page saves feed URL configuration',
        'Content ideas section shows generated ideas',
        'Graceful error handling if a feed URL is unreachable',
    ],
    priority=1,
)
print(f'[{time.time()-t_start:.1f}s] TASK-{task.sequence_number:03d} created (ID: {task.id})')
print()

# ── Instrument phases ─────────────────────────────────────────────
phase_times = {}
original_execute_phase = engine._execute_phase

def timed_phase(phase, ctx):
    t0 = time.time()
    print(f'[{time.time()-t_start:.1f}s] >>> PHASE: {phase.value} starting...')
    result = original_execute_phase(phase, ctx)
    elapsed = time.time() - t0
    phase_times[phase.value] = elapsed
    status = 'PASS' if result[0] else f'FAIL: {result[1]}'
    print(f'[{time.time()-t_start:.1f}s] <<< PHASE: {phase.value} done in {elapsed:.1f}s — {status}')
    print()
    return result

engine._execute_phase = timed_phase

# ── Run ───────────────────────────────────────────────────────────
print('Starting pipeline...')
print()
result = engine.run_task(task.id)

total = time.time() - t_start
print('='*60)
print(f'FORGE RESULT: {"SUCCESS" if result.success else "FAILED"}')
print(f'Total time: {total:.1f}s')
print()
print('Phase breakdown:')
for phase, t in phase_times.items():
    print(f'  {phase:<12} {t:.1f}s')
print()

if result.success:
    # Save the generated dashboard code
    impl = result.output.get('implementation', '')
    if impl:
        # Extract code blocks
        import re
        code_blocks = re.findall(r'```python\n(.*?)```', impl, re.DOTALL)
        if code_blocks:
            dashboard_code = code_blocks[0]
            out_path = Path('/home/claude/forge/workspace/dashboard/dashboard.py')
            out_path.write_text(dashboard_code)
            print(f'Dashboard code saved to: {out_path}')
            print(f'Code length: {len(dashboard_code)} chars, {dashboard_code.count(chr(10))} lines')
        else:
            # Save full implementation
            out_path = Path('/home/claude/forge/workspace/dashboard/dashboard_raw.txt')
            out_path.write_text(impl)
            print(f'Raw output saved: {out_path}')
    
    # Save full results
    results_path = Path('/home/claude/forge/workspace/forge_test_results.json')
    results_path.write_text(json.dumps({
        'task_id': task.id,
        'task_seq': task.sequence_number,
        'success': result.success,
        'total_seconds': round(total, 1),
        'phase_times': {k: round(v, 1) for k, v in phase_times.items()},
        'tokens_used': result.tokens_used,
        'provider': result.provider_used,
        'tests_passed': result.tests_passed,
        'review_passed': result.review_passed,
    }, indent=2))
    print(f'Results saved: {results_path}')
else:
    print(f'ERROR: {result.error}')

