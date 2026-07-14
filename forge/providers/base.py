"""
FORGE Provider Abstraction Layer
=================================
All LLM providers implement BaseProvider. Adding a new provider means
implementing one class — no orchestration changes required.

Providers: Anthropic, OpenAI, Gemini, Ollama, OpenRouter
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from forge.telemetry.logger import get_logger
from forge.telemetry.metrics import get_metrics

log = get_logger("providers")
metrics = get_metrics()


@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str


@dataclass
class ToolCall:
    """A tool invocation returned by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]   # already parsed from JSON


@dataclass
class ToolCall:
    """A tool invocation returned by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]   # already parsed from JSON


@dataclass
class CompletionRequest:
    messages: List[Message]
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.2
    system_prompt: str = ""
    task_id: str = ""
    correlation_id: str = ""
    tools: Optional[List[Dict]] = None        # OpenAI-format tool schemas
    anthropic_tools: Optional[List[Dict]] = None  # Anthropic-format tool schemas
    tools: Optional[List[Dict]] = None        # OpenAI-format tool schemas
    anthropic_tools: Optional[List[Dict]] = None  # Anthropic-format tool schemas


@dataclass
class CompletionResponse:
    content: str
    model: str
    provider: str
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: int = 0
    raw: Optional[dict] = None
    tool_calls: Optional[List["ToolCall"]] = None   # set when LLM wants to call tools

    @property
    def wants_tool_call(self) -> bool:
        return bool(self.tool_calls)
    tool_calls: Optional[List["ToolCall"]] = None   # set when LLM wants to call tools

    @property
    def wants_tool_call(self) -> bool:
        return bool(self.tool_calls)

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output

    @property
    def estimated_cost_usd(self) -> float:
        # Very rough approximations — providers update pricing frequently
        rates = {
            "anthropic":  (3.0,  15.0),   # $/1M tokens in/out
            "openai":     (2.5,  10.0),
            "gemini":     (0.35,  1.05),
            "ollama":     (0.0,   0.0),
            "openrouter": (1.0,   3.0),
            "litellm":    (0.0,   0.0),   # local inference — no token cost
        }
        r = rates.get(self.provider, (1.0, 3.0))
        return (self.tokens_input * r[0] + self.tokens_output * r[1]) / 1_000_000


class ProviderError(Exception):
    def __init__(self, message: str, provider: str, retryable: bool = True, status_code: int = 0):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code


class RateLimitError(ProviderError):
    def __init__(self, provider: str):
        super().__init__(f"Rate limit hit for {provider}", provider, retryable=True, status_code=429)


class AuthError(ProviderError):
    def __init__(self, provider: str):
        super().__init__(f"Authentication failed for {provider}", provider, retryable=False, status_code=401)


class ContextLengthError(ProviderError):
    def __init__(self, provider: str):
        super().__init__(f"Context length exceeded for {provider}", provider, retryable=False, status_code=400)


class BaseProvider(ABC):
    name: str = "base"
    default_model: str = ""

    def __init__(self, api_key: str = "", base_url: str = "", timeout: int = 120):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @abstractmethod
    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Execute a completion request. Raises ProviderError on failure."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is reachable and authenticated."""
        ...

    def _http_post(self, url: str, payload: dict, headers: dict) -> dict:
        """Simple HTTP POST using stdlib urllib."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code == 401:
                raise AuthError(self.name)
            elif e.code == 429:
                raise RateLimitError(self.name)
            elif e.code == 400 and "context" in body.lower():
                raise ContextLengthError(self.name)
            raise ProviderError(
                f"HTTP {e.code}: {body[:200]}",
                provider=self.name,
                retryable=e.code >= 500,
                status_code=e.code,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(str(e), provider=self.name, retryable=True)

    def _http_get(self, url: str, headers: dict) -> dict:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise ProviderError(str(e), provider=self.name, retryable=True)


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-opus-4-5"

    MODELS = [
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ]

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        if request.anthropic_tools:
            payload["tools"] = request.anthropic_tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        start = time.monotonic()
        raw = self._http_post(f"{self.base_url}/v1/messages", payload, headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        # Parse content — may contain text blocks and/or tool_use blocks
        text_parts = []
        tool_calls = []
        for block in raw.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                raw_input = block.get("input", {})
                if isinstance(raw_input, str):
                    try:
                        raw_input = json.loads(raw_input)
                    except Exception:
                        raw_input = {}
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=raw_input,
                ))

        usage = raw.get("usage", {})
        return CompletionResponse(
            content="\n".join(text_parts),
            model=model,
            provider=self.name,
            tokens_input=usage.get("input_tokens", 0),
            tokens_output=usage.get("output_tokens", 0),
            duration_ms=duration_ms,
            raw=raw,
            tool_calls=tool_calls or None,
        )

    def health_check(self) -> bool:
        try:
            r = self.complete(CompletionRequest(
                messages=[Message(role="user", content="ping")],
                max_tokens=10,
            ))
            return bool(r.content)
        except Exception:
            return False


class OpenAIProvider(BaseProvider):
    name = "openai"
    default_model = "gpt-4o"

    MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3"]

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        start = time.monotonic()
        raw = self._http_post(f"{self.base_url}/v1/chat/completions", payload, headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        message = raw["choices"][0]["message"] if raw.get("choices") else {}
        content = message.get("content") or ""

        # Parse tool calls
        tool_calls = []
        for tc in message.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))

        usage = raw.get("usage", {})
        return CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            tokens_input=usage.get("prompt_tokens", 0),
            tokens_output=usage.get("completion_tokens", 0),
            duration_ms=duration_ms,
            raw=raw,
            tool_calls=tool_calls or None,
        )

    def health_check(self) -> bool:
        try:
            r = self.complete(CompletionRequest(
                messages=[Message(role="user", content="ping")],
                max_tokens=10,
            ))
            return bool(r.content)
        except Exception:
            return False


class GeminiProvider(BaseProvider):
    name = "gemini"
    default_model = "gemini-1.5-pro"

    MODELS = ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-pro"]

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        parts = []
        if request.system_prompt:
            parts.append({"text": f"[System]: {request.system_prompt}\n\n"})
        for m in request.messages:
            parts.append({"text": f"[{m.role}]: {m.content}"})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        headers = {"Content-Type": "application/json"}
        url = f"{self.base_url}/v1beta/models/{model}:generateContent?key={self.api_key}"

        start = time.monotonic()
        raw = self._http_post(url, payload, headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            content = raw["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            content = ""

        usage = raw.get("usageMetadata", {})
        return CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            tokens_input=usage.get("promptTokenCount", 0),
            tokens_output=usage.get("candidatesTokenCount", 0),
            duration_ms=duration_ms,
            raw=raw,
        )

    def health_check(self) -> bool:
        try:
            r = self.complete(CompletionRequest(
                messages=[Message(role="user", content="ping")],
                max_tokens=10,
            ))
            return bool(r.content)
        except Exception:
            return False


class OllamaProvider(BaseProvider):
    """Local Ollama provider — works fully offline."""
    name = "ollama"
    default_model = "llama3.2"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        # Ollama uses OpenAI-compatible tool_calls format
        if request.tools:
            payload["tools"] = request.tools

        headers = {"Content-Type": "application/json"}

        start = time.monotonic()
        raw = self._http_post(f"{self.base_url}/api/chat", payload, headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        message = raw.get("message", {})
        content = message.get("content", "") or ""

        # Parse tool calls (Ollama returns same format as OpenAI)
        tool_calls = []
        for tc in message.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", f"call_{len(tool_calls)}"),
                name=fn.get("name", ""),
                arguments=raw_args,
            ))

        usage = raw.get("usage", {})
        return CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            tokens_input=usage.get("prompt_tokens", 0),
            tokens_output=usage.get("completion_tokens", 0),
            duration_ms=duration_ms,
            raw=raw,
            tool_calls=tool_calls or None,
        )

    def health_check(self) -> bool:
        try:
            self._http_get(f"{self.base_url}/api/tags", {})
            return True
        except Exception:
            return False


class OpenRouterProvider(BaseProvider):
    """OpenRouter — routes to many providers through one API."""
    name = "openrouter"
    default_model = "anthropic/claude-opus-4-5"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model = request.model or self.default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://forge.dev",
            "X-Title": "FORGE",
        }

        start = time.monotonic()
        raw = self._http_post(f"{self.base_url}/v1/chat/completions", payload, headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        content = raw["choices"][0]["message"]["content"] if raw.get("choices") else ""
        usage = raw.get("usage", {})
        return CompletionResponse(
            content=content,
            model=model,
            provider=self.name,
            tokens_input=usage.get("prompt_tokens", 0),
            tokens_output=usage.get("completion_tokens", 0),
            duration_ms=duration_ms,
            raw=raw,
        )

    def health_check(self) -> bool:
        try:
            r = self.complete(CompletionRequest(
                messages=[Message(role="user", content="ping")],
                max_tokens=10,
            ))
            return bool(r.content)
        except Exception:
            return False


class LiteLLMProvider(BaseProvider):
    """
    LiteLLM local proxy — OpenAI-compatible API on localhost:4000.

    LiteLLM sits in front of llama-swap, which hot-swaps GGUF models
    loaded by llama.cpp.  The ``model`` field in CompletionRequest must
    be the llama-swap alias (e.g. ``qwen3``, ``deepseek``, ``qwen3vl``),
    not a cloud model name.

    No API key required for local operation.  If LiteLLM is configured
    with cloud fallbacks, pass ``LITELLM_MASTER_KEY`` via env.
    """

    name          = "litellm"
    default_model = "qwen3"   # coding alias — overridden by RouterConfig

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        model    = request.model or self.default_model
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
            "max_tokens":  request.max_tokens,
            "stream":      False,
        }
        if request.tools:
            payload["tools"] = request.tools

        # LiteLLM accepts an optional master key but works key-free locally
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        start = time.monotonic()
        raw   = self._http_post(
            f"{self.base_url}/v1/chat/completions", payload, headers
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        choice  = raw.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""

        # Tool calls (OpenAI format — same as what LiteLLM proxies)
        tool_calls = []
        for tc in message.get("tool_calls", []):
            fn      = tc.get("function", {})
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}
            tool_calls.append(ToolCall(
                id        = tc.get("id", f"call_{len(tool_calls)}"),
                name      = fn.get("name", ""),
                arguments = raw_args,
            ))

        usage = raw.get("usage", {})
        return CompletionResponse(
            content       = content,
            model         = model,
            provider      = self.name,
            tokens_input  = usage.get("prompt_tokens", 0),
            tokens_output = usage.get("completion_tokens", 0),
            duration_ms   = duration_ms,
            raw           = raw,
            tool_calls    = tool_calls or None,
        )

    def health_check(self) -> bool:
        """
        LiteLLM exposes /health for a quick liveness check and
        /v1/models for a list of configured model aliases.
        """
        try:
            self._http_get(f"{self.base_url}/health", {})
            return True
        except Exception:
            return False

    def list_models(self) -> list:
        """Return model aliases registered in LiteLLM."""
        try:
            raw = self._http_get(f"{self.base_url}/v1/models", {})
            return [m.get("id", "") for m in raw.get("data", [])]
        except Exception:
            return []


PROVIDER_CLASSES = {
    "anthropic":  AnthropicProvider,
    "openai":     OpenAIProvider,
    "gemini":     GeminiProvider,
    "ollama":     OllamaProvider,
    "openrouter": OpenRouterProvider,
    "litellm":    LiteLLMProvider,
}


def build_provider(name: str, api_key: str = "", base_url: str = "", timeout: int = 120) -> BaseProvider:
    cls = PROVIDER_CLASSES.get(name)
    if not cls:
        raise ValueError(f"Unknown provider: {name!r}. Available: {list(PROVIDER_CLASSES)}")
    return cls(api_key=api_key, base_url=base_url or cls.__dict__.get("base_url", ""), timeout=timeout)