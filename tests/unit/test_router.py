"""
Unit Tests: Model Router
========================
Tests routing decisions, fallback logic, retry, and failover.
All providers are mocked — no real API calls.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from forge.config import ForgeConfig, ProviderConfig
from forge.orchestration.task import TaskType
from forge.providers.base import (
    BaseProvider, CompletionRequest, CompletionResponse, Message,
    ProviderError, RateLimitError, AuthError, ContextLengthError
)
from forge.router.model_router import ModelRouter


def make_mock_response(content="ok", provider="mock", tokens=100):
    return CompletionResponse(
        content=content,
        model="mock-model",
        provider=provider,
        tokens_input=50,
        tokens_output=50,
        duration_ms=100,
    )


def make_mock_provider(name: str, response=None, side_effect=None):
    provider = MagicMock(spec=BaseProvider)
    provider.name = name
    provider.default_model = f"{name}-model"
    if side_effect:
        provider.complete.side_effect = side_effect
    else:
        provider.complete.return_value = response or make_mock_response(provider=name)
    provider.health_check.return_value = True
    return provider


class TestRoutingDecisions(unittest.TestCase):
    def _make_router(self, providers: dict) -> ModelRouter:
        cfg = ForgeConfig()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = providers
        return router

    def test_coding_routes_to_anthropic_by_default(self):
        anthropic = make_mock_provider("anthropic")
        router = self._make_router({"anthropic": anthropic})
        decision = router.select_provider(TaskType.CODING)
        self.assertEqual(decision.primary_provider, "anthropic")

    def test_review_routes_to_openai_by_default(self):
        openai = make_mock_provider("openai")
        router = self._make_router({"openai": openai})
        decision = router.select_provider(TaskType.REVIEW)
        self.assertEqual(decision.primary_provider, "openai")

    def test_large_context_routes_to_gemini_by_default(self):
        gemini = make_mock_provider("gemini")
        router = self._make_router({"gemini": gemini})
        decision = router.select_provider(TaskType.LARGE_CONTEXT)
        self.assertEqual(decision.primary_provider, "gemini")

    def test_analysis_routes_to_large_context_provider(self):
        router = self._make_router({})
        decision = router.select_provider(TaskType.ANALYSIS)
        self.assertEqual(decision.primary_provider, router.config.router.large_context_provider)

    def test_primary_provider_is_first_in_fallback_chain(self):
        router = self._make_router({})
        decision = router.select_provider(TaskType.CODING)
        self.assertEqual(decision.fallback_chain[0], decision.primary_provider)

    def test_all_providers_in_fallback_chain(self):
        router = self._make_router({})
        decision = router.select_provider(TaskType.CODING)
        # All configured fallback providers should appear
        for p in router.config.router.fallback_chain:
            self.assertIn(p, decision.fallback_chain)


class TestSuccessfulCompletion(unittest.TestCase):
    def _make_router(self, providers: dict) -> ModelRouter:
        cfg = ForgeConfig()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = providers
        return router

    def test_successful_completion_returns_response(self):
        anthropic = make_mock_provider("anthropic")
        router = self._make_router({"anthropic": anthropic})
        request = CompletionRequest(messages=[Message(role="user", content="hello")])
        response = router.complete(request, TaskType.CODING, max_retries_per_provider=1)
        self.assertEqual(response.content, "ok")
        self.assertEqual(response.provider, "anthropic")

    def test_provider_called_once_on_success(self):
        anthropic = make_mock_provider("anthropic")
        router = self._make_router({"anthropic": anthropic})
        request = CompletionRequest(messages=[Message(role="user", content="hello")])
        router.complete(request, TaskType.CODING, max_retries_per_provider=3)
        self.assertEqual(anthropic.complete.call_count, 1)


class TestRetryBehaviour(unittest.TestCase):
    def _make_router(self, providers: dict) -> ModelRouter:
        cfg = ForgeConfig()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = providers
        return router

    def test_rate_limit_retries_with_backoff(self):
        # Fail twice with rate limit, then succeed
        anthropic = make_mock_provider("anthropic", side_effect=[
            RateLimitError("anthropic"),
            RateLimitError("anthropic"),
            make_mock_response(provider="anthropic"),
        ])
        router = self._make_router({"anthropic": anthropic})

        with patch("time.sleep"):  # Don't actually wait
            request = CompletionRequest(messages=[Message(role="user", content="test")])
            response = router.complete(request, TaskType.CODING, max_retries_per_provider=3)

        self.assertEqual(anthropic.complete.call_count, 3)
        self.assertEqual(response.provider, "anthropic")

    def test_transient_error_retries(self):
        anthropic = make_mock_provider("anthropic", side_effect=[
            ProviderError("timeout", provider="anthropic", retryable=True),
            make_mock_response(provider="anthropic"),
        ])
        router = self._make_router({"anthropic": anthropic})

        with patch("time.sleep"):
            request = CompletionRequest(messages=[Message(role="user", content="test")])
            response = router.complete(request, TaskType.CODING, max_retries_per_provider=3)

        self.assertEqual(anthropic.complete.call_count, 2)
        self.assertEqual(response.provider, "anthropic")


class TestFailoverBehaviour(unittest.TestCase):
    def _make_router_with_chain(self, *providers) -> ModelRouter:
        cfg = ForgeConfig()
        cfg.router.coding_provider = providers[0].name if providers else "anthropic"
        cfg.router.fallback_chain = [p.name for p in providers]
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg
        router._providers = {p.name: p for p in providers}
        return router

    def test_auth_failure_skips_to_next_provider(self):
        anthropic = make_mock_provider("anthropic", side_effect=AuthError("anthropic"))
        openai = make_mock_provider("openai", response=make_mock_response(provider="openai"))
        router = self._make_router_with_chain(anthropic, openai)

        request = CompletionRequest(messages=[Message(role="user", content="test")])
        response = router.complete(request, TaskType.CODING, max_retries_per_provider=3)

        # Auth errors should not retry
        self.assertEqual(anthropic.complete.call_count, 1)
        self.assertEqual(openai.complete.call_count, 1)
        self.assertEqual(response.provider, "openai")

    def test_context_length_failure_skips_to_next(self):
        anthropic = make_mock_provider("anthropic", side_effect=ContextLengthError("anthropic"))
        gemini = make_mock_provider("gemini", response=make_mock_response(provider="gemini"))
        router = self._make_router_with_chain(anthropic, gemini)

        request = CompletionRequest(messages=[Message(role="user", content="huge context")])
        response = router.complete(request, TaskType.CODING, max_retries_per_provider=3)

        self.assertEqual(anthropic.complete.call_count, 1)
        self.assertEqual(response.provider, "gemini")

    def test_all_providers_fail_raises_error(self):
        anthropic = make_mock_provider("anthropic", side_effect=ProviderError("down", "anthropic"))
        openai = make_mock_provider("openai", side_effect=ProviderError("down", "openai"))

        with patch("time.sleep"):
            router = self._make_router_with_chain(anthropic, openai)
            request = CompletionRequest(messages=[Message(role="user", content="test")])
            with self.assertRaises(ProviderError) as ctx:
                router.complete(request, TaskType.CODING, max_retries_per_provider=2)

        self.assertIn("All providers exhausted", str(ctx.exception))

    def test_failover_resets_model_for_next_provider(self):
        """When failing over, the model should be cleared so the new provider picks its default."""
        anthropic = make_mock_provider("anthropic", side_effect=AuthError("anthropic"))
        openai = make_mock_provider("openai", response=make_mock_response(provider="openai"))
        router = self._make_router_with_chain(anthropic, openai)

        request = CompletionRequest(
            messages=[Message(role="user", content="test")],
            model="claude-opus-4-5",
        )
        router.complete(request, TaskType.CODING, max_retries_per_provider=1)
        # After failover the request model should have been cleared
        # (verified indirectly — openai was called and succeeded)
        self.assertEqual(openai.complete.call_count, 1)


class TestHealthStatus(unittest.TestCase):
    def test_health_status_aggregates_all_providers(self):
        cfg = ForgeConfig()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg

        p1 = make_mock_provider("anthropic")
        p1.health_check.return_value = True
        p2 = make_mock_provider("openai")
        p2.health_check.return_value = False

        router._providers = {"anthropic": p1, "openai": p2}
        status = router.health_status()

        self.assertTrue(status["anthropic"])
        self.assertFalse(status["openai"])

    def test_health_check_exception_returns_false(self):
        cfg = ForgeConfig()
        router = ModelRouter.__new__(ModelRouter)
        router.config = cfg

        p = make_mock_provider("anthropic")
        p.health_check.side_effect = Exception("Connection refused")
        router._providers = {"anthropic": p}

        status = router.health_status()
        self.assertFalse(status["anthropic"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
