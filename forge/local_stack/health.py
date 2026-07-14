"""
FORGE Thunderbird — Local Stack Health
=======================================
Checks whether each component of the local LLM stack is running
and responsive. Used by `forge stack health` and `forge health`.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, Optional

from forge.telemetry.logger import get_logger

log = get_logger("local_stack.health")

_TIMEOUT = 3   # seconds per health check


@dataclass
class ComponentStatus:
    name:      str
    running:   bool
    url:       str
    error:     str = ""
    models:    list = field(default_factory=list)
    detail:    dict = field(default_factory=dict)


class LocalStackHealth:
    """
    Checks liveness of llama-swap, LiteLLM, and the currently
    loaded llama-server model.
    """

    def __init__(
        self,
        llama_swap_port:  int = 8080,
        litellm_port:     int = 4000,
        llama_server_port:int = 8081,
    ):
        self._swap_url   = f"http://127.0.0.1:{llama_swap_port}"
        self._litellm_url= f"http://127.0.0.1:{litellm_port}"
        self._server_url = f"http://127.0.0.1:{llama_server_port}"

    def check_litellm(self) -> ComponentStatus:
        url = f"{self._litellm_url}/health"
        try:
            raw = _get(url)
            # Also fetch model list
            models = []
            try:
                model_raw = _get(f"{self._litellm_url}/v1/models")
                models = [m.get("id","") for m in model_raw.get("data", [])]
            except Exception:
                pass
            return ComponentStatus(
                name="LiteLLM", running=True, url=url,
                models=models, detail=raw,
            )
        except Exception as exc:
            return ComponentStatus(
                name="LiteLLM", running=False, url=url,
                error=str(exc),
            )

    def check_llama_swap(self) -> ComponentStatus:
        url = f"{self._swap_url}/health"
        try:
            raw = _get(url)
            return ComponentStatus(
                name="llama-swap", running=True, url=url, detail=raw,
            )
        except Exception as exc:
            return ComponentStatus(
                name="llama-swap", running=False, url=url,
                error=str(exc),
            )

    def check_llama_server(self) -> ComponentStatus:
        """Check the currently active llama-server instance."""
        url = f"{self._server_url}/health"
        try:
            raw = _get(url)
            # /props gives the loaded model name
            props = {}
            try:
                props = _get(f"{self._server_url}/props")
            except Exception:
                pass
            return ComponentStatus(
                name="llama-server", running=True, url=url,
                detail={**raw, **props},
            )
        except Exception as exc:
            return ComponentStatus(
                name="llama-server", running=False, url=url,
                error=str(exc),
            )

    def full_report(self) -> Dict[str, ComponentStatus]:
        """Check all components. Returns dict keyed by component name."""
        return {
            "litellm":      self.check_litellm(),
            "llama_swap":   self.check_llama_swap(),
            "llama_server": self.check_llama_server(),
        }

    def all_healthy(self) -> bool:
        report = self.full_report()
        return all(s.running for s in report.values())

    def format_report(self) -> str:
        """Formatted string for `forge stack health` output."""
        report = self.full_report()
        lines  = []
        for name, status in report.items():
            icon = "✓" if status.running else "✗"
            color_on  = "\033[32m" if status.running else "\033[31m"
            color_off = "\033[0m"
            line = f"  {color_on}{icon}{color_off} {status.name:<16} {status.url}"
            if not status.running:
                line += f"  — {status.error}"
            elif status.models:
                line += f"  models: {', '.join(status.models[:5])}"
            elif status.detail:
                loaded = status.detail.get("model", status.detail.get("model_path", ""))
                if loaded:
                    line += f"  loaded: {loaded}"
            lines.append(line)
        return "\n".join(lines)


def _get(url: str) -> dict:
    """Simple JSON GET with short timeout."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))
