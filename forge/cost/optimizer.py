"""
FORGE Cost & Token Efficiency Optimizer
========================================
Three subsystems:

1. ModelSelector      — automatically route tasks to the cheapest model that
                        meets the quality threshold for that task type/complexity
2. InferenceCache     — cache completed LLM calls; reuse on identical or
                        near-identical prompts (hash-based exact match)
3. TokenBudgetTracker — track spend per task/phase/provider; warn when
                        a task is burning unexpectedly many tokens

Env vars
--------
  FORGE_COST_OPTIMIZE=true      enable automatic model selection (default: false)
  FORGE_CACHE_ENABLED=true      enable inference cache (default: false)
  FORGE_TOKEN_BUDGET=50000      warn when a task exceeds this many tokens
  FORGE_MAX_COST_USD=1.00       warn when a task exceeds this cost in USD
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from forge.telemetry.logger import get_logger

log = get_logger("cost")

_COST_OPTIMIZE  = os.environ.get("FORGE_COST_OPTIMIZE",  "false").lower() == "true"
_CACHE_ENABLED  = os.environ.get("FORGE_CACHE_ENABLED",  "false").lower() == "true"
_TOKEN_BUDGET   = int(os.environ.get("FORGE_TOKEN_BUDGET", "50000"))
_MAX_COST_USD   = float(os.environ.get("FORGE_MAX_COST_USD", "1.00"))


# ── Pricing table (configurable, not hardcoded) ───────────────────────────────
# Format: {provider: {model: (input_per_1m, output_per_1m)}} in USD
# Override by setting FORGE_PRICING_JSON env var with a JSON string

_DEFAULT_PRICING: Dict[str, Dict[str, Tuple[float, float]]] = {
    "anthropic": {
        "claude-opus-4-6":    (15.00, 75.00),
        "claude-sonnet-4-6":   (3.00, 15.00),
        "claude-opus-4-5":    (15.00, 75.00),
        "claude-sonnet-4-5":   (3.00, 15.00),
        "claude-haiku-4-5":    (0.25,  1.25),
        "_default":            (3.00, 15.00),
    },
    "openai": {
        "gpt-4o":              (2.50, 10.00),
        "gpt-4o-mini":         (0.15,  0.60),
        "gpt-4-turbo":         (10.0, 30.00),
        "o1":                  (15.0, 60.00),
        "o3":                  (10.0, 40.00),
        "_default":            (2.50, 10.00),
    },
    "gemini": {
        "gemini-2.0-flash":    (0.10,  0.40),
        "gemini-2.0-pro":      (1.25,  5.00),
        "gemini-1.5-pro":      (1.25,  5.00),
        "gemini-1.5-flash":    (0.075, 0.30),
        "_default":            (0.35,  1.05),
    },
    "ollama": {
        "_default":            (0.0,   0.0),
    },
    "openrouter": {
        "_default":            (1.00,  3.00),
    },
}


def get_pricing() -> Dict[str, Dict[str, Tuple[float, float]]]:
    """Return pricing table — FORGE_PRICING_JSON env var overrides defaults."""
    custom = os.environ.get("FORGE_PRICING_JSON")
    if custom:
        try:
            return json.loads(custom)
        except Exception:
            log.warning("FORGE_PRICING_JSON is invalid JSON — using defaults")
    return _DEFAULT_PRICING


def estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    """Calculate cost in USD for a given call."""
    pricing = get_pricing()
    provider_pricing = pricing.get(provider, {"_default": (1.0, 3.0)})
    rates = provider_pricing.get(model) or provider_pricing.get("_default", (1.0, 3.0))
    return (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000


# ── Model capability tiers ────────────────────────────────────────────────────
# Each tier lists (provider, model) pairs ordered by cost (cheapest first).
# The optimizer picks the cheapest tier that meets complexity requirements.

@dataclass
class ModelTier:
    name: str
    min_complexity: int    # minimum task complexity score (0-10) for this tier
    providers: List[Tuple[str, str]]  # (provider, model) ordered cheapest first
    quality_score: int     # estimated output quality 1-10


MODEL_TIERS = [
    ModelTier(
        name="minimal",
        min_complexity=0,
        providers=[
            ("ollama",    "gpt-oss:20b"),
            ("ollama",    "llama3.2"),
            ("gemini",    "gemini-1.5-flash"),
            ("openai",    "gpt-4o-mini"),
        ],
        quality_score=6,
    ),
    ModelTier(
        name="standard",
        min_complexity=4,
        providers=[
            ("gemini",    "gemini-2.0-flash"),
            ("openai",    "gpt-4o"),
            ("anthropic", "claude-haiku-4-5"),
        ],
        quality_score=8,
    ),
    ModelTier(
        name="premium",
        min_complexity=7,
        providers=[
            ("anthropic", "claude-sonnet-4-6"),
            ("openai",    "gpt-4o"),
            ("gemini",    "gemini-2.0-pro"),
        ],
        quality_score=9,
    ),
    ModelTier(
        name="maximum",
        min_complexity=9,
        providers=[
            ("anthropic", "claude-opus-4-6"),
            ("openai",    "o1"),
        ],
        quality_score=10,
    ),
]


def estimate_task_complexity(description: str, task_type: str) -> int:
    """
    Heuristic complexity score 0-10 based on task description and type.
    Used to select the appropriate model tier.

    Not perfect — but dramatically better than always using the most expensive model.
    """
    score = 3  # baseline

    desc_lower = description.lower()

    # Task type base scores
    type_scores = {
        "planning":      2,
        "documentation": 2,
        "review":        3,
        "testing":       4,
        "security":      5,
        "coding":        4,
        "analysis":      6,
    }
    score = type_scores.get(task_type, 4)

    # Complexity signals in description
    complexity_signals = [
        ("architect", 3), ("distributed", 3), ("microservice", 2),
        ("concurrent", 2), ("async", 1), ("optimize", 2),
        ("algorithm", 2), ("machine learning", 3), ("neural", 3),
        ("security", 2), ("authentication", 2), ("encryption", 2),
        ("migration", 2), ("refactor", 1), ("design pattern", 2),
        ("scalable", 2), ("performance", 2), ("benchmark", 2),
        ("complex", 1), ("advanced", 1), ("production", 1),
        ("enterprise", 2), ("large-scale", 2),
    ]
    for signal, weight in complexity_signals:
        if signal in desc_lower:
            score += weight

    # Length signal (longer description = more complex)
    words = len(description.split())
    if words > 200: score += 2
    elif words > 100: score += 1

    return min(score, 10)


class ModelSelector:
    """
    Automatically selects the cheapest provider/model that meets quality
    requirements for a given task complexity.

    Falls back to the configured primary provider if FORGE_COST_OPTIMIZE=false.
    """

    def select(
        self,
        task_type: str,
        description: str,
        available_providers: List[str],
        min_quality: int = 7,
    ) -> Optional[Tuple[str, str]]:
        """
        Returns (provider, model) or None if optimization is disabled.

        Only returns providers that are in available_providers (i.e. have keys set).
        """
        if not _COST_OPTIMIZE:
            return None

        complexity = estimate_task_complexity(description, task_type)
        log.info("Task complexity estimated", complexity=complexity, task_type=task_type)

        # Find cheapest tier meeting quality threshold
        eligible_tiers = [t for t in MODEL_TIERS if t.quality_score >= min_quality]
        eligible_tiers.sort(key=lambda t: t.quality_score)  # cheapest quality first

        for tier in eligible_tiers:
            for provider, model in tier.providers:
                if provider in available_providers:
                    log.info(
                        "Model selected by optimizer",
                        provider=provider, model=model,
                        tier=tier.name, complexity=complexity,
                    )
                    return (provider, model)

        return None  # no suitable provider found


# ── Inference Cache ───────────────────────────────────────────────────────────

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_cache (
    cache_key   TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response    TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_used   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_hash ON inference_cache(prompt_hash);
"""


class InferenceCache:
    """
    Hash-based exact-match cache for LLM completions.

    Cache key = SHA256(provider + model + system_prompt + all_messages).
    On hit: returns cached response with zero cost.
    On miss: caller executes normally, then calls store().

    Only activated when FORGE_CACHE_ENABLED=true.
    Cache entries never expire by default — clear manually if needed.
    """

    def __init__(self, db_path: str = "forge_tasks.db"):
        self.enabled = _CACHE_ENABLED
        self.db_path = db_path
        self._lock = threading.Lock()
        if self.enabled:
            self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(CACHE_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _make_key(self, provider: str, model: str, system: str, messages: list) -> str:
        """Deterministic hash of all inputs."""
        payload = json.dumps({
            "provider": provider,
            "model": model,
            "system": system,
            "messages": messages,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, provider: str, model: str, system: str, messages: list) -> Optional[dict]:
        """Return cached response dict or None on miss."""
        if not self.enabled:
            return None
        key = self._make_key(provider, model, system, messages)
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM inference_cache WHERE cache_key=?", (key,)
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE inference_cache SET hits=hits+1, last_used=? WHERE cache_key=?",
                        (datetime.now(timezone.utc).isoformat(), key)
                    )
                    conn.commit()
                    log.info("Cache hit", provider=provider, model=model, key=key[:16])
                    return json.loads(row["response"])
                return None
            finally:
                conn.close()

    def store(
        self,
        provider: str,
        model: str,
        system: str,
        messages: list,
        response_data: dict,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Store a completed LLM response in the cache."""
        if not self.enabled:
            return
        key = self._make_key(provider, model, system, messages)
        now = datetime.now(timezone.utc).isoformat()
        cost = estimate_cost(provider, model, tokens_in, tokens_out)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO inference_cache
                        (cache_key, provider, model, prompt_hash, response,
                         tokens_in, tokens_out, cost_usd, hits, created_at, last_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """, (key, provider, model, key[:16],
                      json.dumps(response_data), tokens_in, tokens_out,
                      cost, now, now))
                conn.commit()
                log.info("Cached response", provider=provider, model=model)
            finally:
                conn.close()

    def stats(self) -> dict:
        """Return cache statistics."""
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            conn = self._conn()
            try:
                total = conn.execute("SELECT COUNT(*) FROM inference_cache").fetchone()[0]
                hits  = conn.execute("SELECT SUM(hits) FROM inference_cache").fetchone()[0] or 0
                saved = conn.execute("SELECT SUM(cost_usd * hits) FROM inference_cache").fetchone()[0] or 0.0
                return {"enabled": True, "entries": total, "total_hits": hits,
                        "estimated_saved_usd": round(saved, 4)}
            finally:
                conn.close()

    def clear(self) -> int:
        """Clear all cache entries. Returns count deleted."""
        if not self.enabled:
            return 0
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM inference_cache")
                conn.commit()
                log.info("Cache cleared", count=cur.rowcount)
                return cur.rowcount
            finally:
                conn.close()


# ── Token Budget Tracker ──────────────────────────────────────────────────────

BUDGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    phase       TEXT NOT NULL,
    agent       TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_task ON token_usage(task_id);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON token_usage(provider);
CREATE INDEX IF NOT EXISTS idx_usage_recorded ON token_usage(recorded_at);
"""


class TokenBudgetTracker:
    """
    Records every LLM call with full cost attribution.
    Warns when tasks exceed configured token/cost budgets.
    Powers the `forge metrics --cost` command.
    """

    def __init__(self, db_path: str = "forge_tasks.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(BUDGET_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def record(
        self,
        task_id: str,
        phase: str,
        agent: str,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        duration_ms: int = 0,
    ) -> dict:
        """Record a completed LLM call. Returns {tokens, cost, warnings}."""
        cost = estimate_cost(provider, model, tokens_in, tokens_out)
        now  = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._conn()
            try:
                conn.execute("""
                    INSERT INTO token_usage
                        (task_id, phase, agent, provider, model,
                         tokens_in, tokens_out, cost_usd, duration_ms, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (task_id, phase, agent, provider, model,
                      tokens_in, tokens_out, cost, duration_ms, now))
                conn.commit()
            finally:
                conn.close()

        # Check budgets
        warnings = []
        task_totals = self.task_summary(task_id)
        if task_totals["total_tokens"] > _TOKEN_BUDGET:
            warnings.append(
                f"Task {task_id} has used {task_totals['total_tokens']:,} tokens "
                f"(budget: {_TOKEN_BUDGET:,})"
            )
        if task_totals["total_cost_usd"] > _MAX_COST_USD:
            warnings.append(
                f"Task {task_id} has cost ${task_totals['total_cost_usd']:.4f} "
                f"(budget: ${_MAX_COST_USD:.2f})"
            )
        for w in warnings:
            log.warning(w)

        return {
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": cost, "warnings": warnings,
        }

    def task_summary(self, task_id: str) -> dict:
        """Return aggregated token/cost stats for a task."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("""
                    SELECT
                        SUM(tokens_in + tokens_out) as total_tokens,
                        SUM(tokens_in)              as tokens_in,
                        SUM(tokens_out)             as tokens_out,
                        SUM(cost_usd)               as total_cost_usd,
                        COUNT(*)                    as call_count
                    FROM token_usage WHERE task_id=?
                """, (task_id,)).fetchone()
                phases = conn.execute("""
                    SELECT phase, agent, provider, model,
                           SUM(tokens_in+tokens_out) as tokens,
                           SUM(cost_usd) as cost
                    FROM token_usage WHERE task_id=?
                    GROUP BY phase, agent
                    ORDER BY recorded_at
                """, (task_id,)).fetchall()
                return {
                    "task_id":       task_id,
                    "total_tokens":  row["total_tokens"] or 0,
                    "tokens_in":     row["tokens_in"] or 0,
                    "tokens_out":    row["tokens_out"] or 0,
                    "total_cost_usd": round(row["total_cost_usd"] or 0, 6),
                    "call_count":    row["call_count"] or 0,
                    "by_phase": [dict(r) for r in phases],
                }
            finally:
                conn.close()

    def provider_summary(self, days: int = 30) -> List[dict]:
        """Return cost breakdown by provider over the last N days."""
        cutoff = datetime.now(timezone.utc).isoformat()[:10]  # YYYY-MM-DD
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("""
                    SELECT provider, model,
                           SUM(tokens_in+tokens_out) as tokens,
                           SUM(cost_usd)             as cost,
                           COUNT(*)                  as calls
                    FROM token_usage
                    WHERE recorded_at >= ?
                    GROUP BY provider, model
                    ORDER BY cost DESC
                """, (cutoff[:8] + "01",)).fetchall()  # rough month boundary
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def format_cost_report(self, task_id: Optional[str] = None) -> str:
        """Format a human-readable cost report for CLI output."""
        lines = ["", "  ── Cost & Token Report ──────────────────────────────────────"]

        if task_id:
            s = self.task_summary(task_id)
            lines += [
                f"  Task:          {task_id}",
                f"  Total tokens:  {s['total_tokens']:,}  "
                f"(in: {s['tokens_in']:,}  out: {s['tokens_out']:,})",
                f"  Estimated cost: ${s['total_cost_usd']:.6f} USD",
                f"  LLM calls:     {s['call_count']}",
            ]
            if s["by_phase"]:
                lines.append("\n  By phase:")
                for p in s["by_phase"]:
                    lines.append(
                        f"    {p['phase']:<12} {p['agent']:<14} "
                        f"{p['tokens']:>8,} tokens  ${p['cost']:.6f}  [{p['provider']}/{p['model']}]"
                    )
        else:
            providers = self.provider_summary()
            if providers:
                lines.append("\n  By provider (this month):")
                for p in providers:
                    lines.append(
                        f"    {p['provider']:<12} {p['model']:<30} "
                        f"{p['tokens']:>10,} tokens  ${p['cost']:.4f}  ({p['calls']} calls)"
                    )
            cache_s = get_cache().stats()
            if cache_s.get("enabled"):
                lines.append(
                    f"\n  Cache: {cache_s['entries']} entries  "
                    f"{cache_s['total_hits']} hits  "
                    f"~${cache_s['estimated_saved_usd']:.4f} saved"
                )

        lines.append("")
        return "\n".join(lines)


# ── Singletons ────────────────────────────────────────────────────────────────

_selector: Optional[ModelSelector] = None
_cache:    Optional[InferenceCache] = None
_tracker:  Optional[TokenBudgetTracker] = None


def get_selector() -> ModelSelector:
    global _selector
    if _selector is None:
        _selector = ModelSelector()
    return _selector


def get_cache(db_path: str = "forge_tasks.db") -> InferenceCache:
    global _cache
    if _cache is None:
        _cache = InferenceCache(db_path)
    return _cache


def get_tracker(db_path: str = "forge_tasks.db") -> TokenBudgetTracker:
    global _tracker
    if _tracker is None:
        _tracker = TokenBudgetTracker(db_path)
    return _tracker