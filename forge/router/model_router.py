"""
FORGE Model Router
==================
Routes requests to the right provider based on task type.
Implements retry, exponential backoff, and cascading failover.
 
Strategy:
  1. Select primary provider for task type
  2. Retry with backoff on transient failure
  3. Failover to next provider in chain
  4. Log every decision for auditability
  5. Preserve state across failover
"""
from __future__ import annotations
 
import time
from dataclasses import dataclass
from typing import Dict, List, Optional
 
from forge.config import ForgeConfig, get_config
from forge.orchestration.task import TaskType
from forge.providers.base import (
    AuthError,
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    ContextLengthError,
    ProviderError,
    RateLimitError,
    build_provider,
)
from forge.telemetry.logger import get_logger
from forge.telemetry.metrics import get_metrics
from forge.cost.optimizer import get_cache
 
log = get_logger("router")
metrics = get_metrics()
 
 
@dataclass
class RoutingDecision:
    task_type: str
    primary_provider: str
    fallback_chain: List[str]
    selected_model: str
 
 
class ModelRouter:
    """
    Routes LLM requests to appropriate providers with retry and failover.

    On initialisation, loads routing recommendations from the memory store
    (written by LearningEngine after each task) and uses them to reorder
    the fallback chain so the historically most-reliable provider is tried
    first for each task type.

    Configuration example (env vars):
        FORGE_CODING_PROVIDER=anthropic
        FORGE_REVIEW_PROVIDER=openai
        FORGE_FALLBACK_CHAIN=anthropic,openai,gemini,ollama
    """

    # Class-level defaults ensure instances created via __new__() (e.g. in
    # unit-test helpers that bypass __init__) always have these attributes.
    _providers:           Dict[str, BaseProvider] = {}
    _learned_preferences: Dict[str, str]          = {}

    def __init__(self, config: Optional[ForgeConfig] = None):
        self.config = config or get_config()
        self._providers: Dict[str, BaseProvider] = {}
        self._learned_preferences: Dict[str, str] = {}   # task_type → preferred provider
        self._build_providers()
        self._load_learned_preferences()
 
    def _build_providers(self):
        cfg = self.config
        for name, p_cfg in cfg.providers.items():
            base_url_defaults = {
                "anthropic": "https://api.anthropic.com",
                "openai": "https://api.openai.com",
                "gemini": "https://generativelanguage.googleapis.com",
                "ollama": "http://localhost:11434",
                "openrouter": "https://openrouter.ai/api",
            }
            try:
                provider = build_provider(
                    name=name,
                    api_key=p_cfg.api_key or "",
                    base_url=p_cfg.base_url or base_url_defaults.get(name, ""),
                    timeout=p_cfg.timeout_seconds,
                )
                self._providers[name] = provider
                log.info("Provider loaded", provider=name)
            except Exception as e:
                log.warning("Provider failed to load", provider=name, error=str(e))

    def _load_learned_preferences(self) -> None:
        """
        Read model_profile memories written by LearningEngine and build a
        task_type → preferred_provider mapping.  This closes the
        meta-learning feedback loop: every completed task updates the
        LearningEngine knowledge base, which is then read here on the next
        router initialisation.

        Non-fatal: any failure leaves _learned_preferences empty and routing
        falls back to the static config-driven chain.
        """
        try:
            from forge.memory.store import get_memory_store
            store = get_memory_store()
            entries = store.recall_all(memory_type="fact", max_results=50)
            for e in entries:
                if not e.key.startswith("model_profile:"):
                    continue
                # Key format: model_profile:<provider>:<task_type>
                parts = e.key.split(":", 2)
                if len(parts) < 3:
                    continue
                provider, task_type = parts[1], parts[2]
                # Only override if confidence is high enough
                if e.confidence >= 0.7:
                    # Keep the highest-confidence entry per task_type
                    existing_key = self._learned_preferences.get(task_type)
                    if existing_key is None:
                        self._learned_preferences[task_type] = provider
                        log.info(
                            "Learned routing preference",
                            task_type=task_type,
                            provider=provider,
                            confidence=e.confidence,
                        )
        except Exception as exc:
            log.debug("Could not load learned preferences (non-fatal)", error=str(exc))
 
    def select_provider(self, task_type: TaskType) -> RoutingDecision:
        """Choose the primary provider and fallback chain for a task type.

        When the local stack is enabled, litellm is the primary provider
        for coding/planning/analysis tasks, with cloud providers as fallback.
        The model alias (qwen3/deepseek/qwen3vl) is embedded in the decision
        and flows through CompletionRequest.model to LiteLLM → llama-swap.
        """
        rcfg = self.config.router
        type_map = {
            TaskType.CODING:       rcfg.coding_provider,
            TaskType.REVIEW:       rcfg.review_provider,
            TaskType.LARGE_CONTEXT:rcfg.large_context_provider,
            TaskType.ANALYSIS:     rcfg.reasoning_provider or rcfg.large_context_provider,
            TaskType.TESTING:      rcfg.coding_provider,
            TaskType.SECURITY:     rcfg.review_provider,
            TaskType.DOCUMENTATION:rcfg.coding_provider,
            TaskType.PLANNING:     rcfg.reasoning_provider or rcfg.coding_provider,
            TaskType.DEBUGGING:    rcfg.reasoning_provider or rcfg.coding_provider,
        }
        primary = type_map.get(task_type, rcfg.default_provider)

        # Apply learned preference if available (meta-learning feedback)
        learned = self._learned_preferences.get(task_type.value)
        if learned and learned in self._providers:
            primary = learned
            log.info("Using learned provider preference",
                     task_type=task_type.value, provider=learned)

        # Build fallback chain: primary first, then rest
        chain = [primary]
        for p in rcfg.fallback_chain:
            if p not in chain:
                chain.append(p)

        model = self._default_model_for(primary, task_type)
        decision = RoutingDecision(
            task_type=task_type.value,
            primary_provider=primary,
            fallback_chain=chain,
            selected_model=model,
        )
        log.info("Routing decision", task_type=task_type.value,
                 primary=primary, model=model, chain=chain)
        return decision
 
    def _default_model_for(
        self,
        provider_name: str,
        task_type: "TaskType | None" = None,
    ) -> str:
        """Return model/alias to use for a provider.

        For litellm, the alias depends on task_type (coding → qwen3,
        planning/analysis → deepseek, vision → qwen3vl).
        Config overrides take precedence over auto-selection.
        """
        from forge.orchestration.task import TaskType as TT
        rcfg = self.config.router

        # litellm: resolve alias from task_type role
        if provider_name == "litellm":
            # Explicit override wins
            if rcfg.litellm_model:
                return rcfg.litellm_model
            if task_type is not None:
                if task_type in (TT.PLANNING, TT.ANALYSIS):
                    return rcfg.reasoning_model
                if task_type == TT.DOCUMENTATION:
                    return rcfg.coding_model
            return rcfg.coding_model   # default alias

        override_map = {
            "anthropic":  rcfg.anthropic_model,
            "openai":     rcfg.openai_model,
            "gemini":     rcfg.gemini_model,
            "ollama":     rcfg.ollama_model,
            "openrouter": rcfg.openrouter_model,
        }
        override = override_map.get(provider_name, "")
        if override:
            return override
        provider = self._providers.get(provider_name)
        if provider:
            return getattr(provider, "default_model", "")
        return ""
 
    def complete(
        self,
        request: CompletionRequest,
        task_type: TaskType = TaskType.CODING,
        max_retries_per_provider: Optional[int] = None,
    ) -> CompletionResponse:
        """
        Execute a completion with retry + failover.
 
        Each provider gets max_retries_per_provider attempts with
        exponential backoff before moving to the next in the chain.
        Defaults to FORGE_MAX_RETRIES env var, or 1 if not set.
        """
        if max_retries_per_provider is None:
            import os as _os
            max_retries_per_provider = int(_os.environ.get("FORGE_MAX_RETRIES", "1"))
        decision = self.select_provider(task_type)
        chain = decision.fallback_chain
 
        last_error: Optional[Exception] = None
 
        for provider_name in chain:
            provider = self._providers.get(provider_name)
            if not provider:
                log.warning("Provider not configured", provider=provider_name)
                continue
 
            if not request.model:
                request.model = self._default_model_for(provider_name, task_type)
 
            for attempt in range(1, max_retries_per_provider + 1):
                try:
                    log.info(
                        "Attempting completion",
                        provider=provider_name,
                        model=request.model,
                        attempt=attempt,
                        task_id=request.task_id,
                    )
                    metrics.inc("forge_llm_requests_total")

                    # Check inference cache before calling provider
                    _cache = get_cache()
                    _msgs  = [{"role": m.role, "content": m.content} for m in request.messages]
                    _cached = _cache.get(provider_name, request.model or "",
                                         request.system_prompt, _msgs)
                    start = time.monotonic()
                    if _cached:
                        from forge.providers.base import CompletionResponse
                        response = CompletionResponse(
                            content=_cached.get("content", ""),
                            model=request.model or "", provider=provider_name,
                            tokens_input=_cached.get("tokens_input", 0),
                            tokens_output=_cached.get("tokens_output", 0),
                        )
                        metrics.inc("forge_cache_hits_total")
                        log.info("Inference cache hit", provider=provider_name)
                    else:
                        response = provider.complete(request)
                        _cache.store(provider_name, request.model or "",
                                     request.system_prompt, _msgs,
                                     {"content": response.content,
                                      "tokens_input": response.tokens_input,
                                      "tokens_output": response.tokens_output},
                                     response.tokens_input, response.tokens_output)
                    elapsed = time.monotonic() - start
 
                    metrics.observe("forge_llm_latency_seconds", elapsed)
                    metrics.inc("forge_tokens_used_total", response.tokens_total)
                    metrics.inc("forge_cost_usd_total", response.estimated_cost_usd)
 
                    log.info(
                        "Completion success",
                        provider=provider_name,
                        model=request.model,
                        tokens=response.tokens_total,
                        duration_ms=response.duration_ms,
                        cost_usd=round(response.estimated_cost_usd, 6),
                    )
                    return response
 
                except AuthError as e:
                    log.error("Auth error — skipping provider", provider=provider_name, error=str(e))
                    metrics.inc("forge_llm_failures_total")
                    last_error = e
                    break  # Don't retry auth failures
 
                except ContextLengthError as e:
                    log.warning("Context too long — skipping provider", provider=provider_name)
                    metrics.inc("forge_llm_failures_total")
                    last_error = e
                    break  # Don't retry context failures
 
                except RateLimitError as e:
                    wait = 2 ** attempt
                    log.warning("Rate limited", provider=provider_name, wait_seconds=wait, attempt=attempt)
                    metrics.inc("forge_llm_retries_total")
                    last_error = e
                    if attempt < max_retries_per_provider:
                        time.sleep(wait)
 
                except ProviderError as e:
                    wait = min(2 ** attempt, 30)
                    log.warning(
                        "Provider error",
                        provider=provider_name,
                        error=str(e),
                        retryable=e.retryable,
                        attempt=attempt,
                    )
                    metrics.inc("forge_llm_failures_total")
                    metrics.inc("forge_llm_retries_total")
                    last_error = e
                    if not e.retryable or attempt >= max_retries_per_provider:
                        break
                    time.sleep(wait)
 
                except Exception as e:
                    log.error("Unexpected error", provider=provider_name, error=str(e))
                    metrics.inc("forge_llm_failures_total")
                    last_error = e
                    break
 
            # If we get here, this provider exhausted its retries
            if provider_name != chain[-1]:
                log.warning("Failing over", from_provider=provider_name, error=str(last_error))
                metrics.inc("forge_llm_failovers_total")
                request.model = ""  # Reset model for next provider
 
        raise ProviderError(
            f"All providers exhausted. Last error: {last_error}",
            provider="all",
            retryable=False,
        )
 
    def health_status(self) -> Dict[str, bool]:
        """Check health of all configured providers."""
        results = {}
        for name, provider in self._providers.items():
            try:
                results[name] = provider.health_check()
            except Exception:
                results[name] = False
        return results
 
    def available_providers(self) -> List[str]:
        return list(self._providers.keys())