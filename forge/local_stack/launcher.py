"""
FORGE Thunderbird — Stack Launcher
====================================
Starts and stops llama-swap and LiteLLM as managed subprocesses.
Writes PIDs to a state file so `forge stack stop` can clean up
even if the user opened a new terminal session.

Startup order (must be sequential — each depends on the previous):
  1. Generate configs (if auto_generate_configs is True)
  2. Start llama-swap  → waits for /health on llama_swap_port
  3. Start LiteLLM     → waits for /health on litellm_port

llama-server is NOT started directly — llama-swap starts it on
first model request (lazy loading). This is the designed behaviour.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from forge.telemetry.logger import get_logger
from forge.local_stack.health import LocalStackHealth, _get

log = get_logger("local_stack.launcher")

_STATE_FILE     = Path(".forge_stack_pids.json")
_STARTUP_TIMEOUT= 30   # seconds to wait for each component to become healthy
_POLL_INTERVAL  = 0.5  # seconds between health poll attempts


class StackLauncher:
    """
    Manages the lifecycle of llama-swap and LiteLLM processes.
    """

    def __init__(self, local_stack_cfg, output_dir: str = "."):
        self._cfg        = local_stack_cfg
        self._output_dir = Path(output_dir)
        self._health     = LocalStackHealth(
            llama_swap_port   = local_stack_cfg.llama_swap_port,
            litellm_port      = local_stack_cfg.litellm_port,
            llama_server_port = local_stack_cfg.llama_server_port,
        )

    def start(self) -> dict:
        """
        Generate configs (if needed), then start llama-swap and LiteLLM.
        Returns a status dict with PIDs and health status.
        """
        # ── Step 1: Generate configs ──────────────────────────────────────
        if self._cfg.auto_generate_configs:
            from forge.local_stack.model_registry import get_registry
            from forge.local_stack.config_generator import generate_all
            registry = get_registry()
            if not registry.profiles:
                raise RuntimeError(
                    "No GGUF models found. Check FORGE_MODELS_DIR in .env\n"
                    f"  Searched: {self._cfg.models_dirs}"
                )
            paths = generate_all(registry, self._cfg, str(self._output_dir))
            log.info("Configs generated", **paths)

        # ── Step 2: Start llama-swap ──────────────────────────────────────
        swap_pid = self._start_llama_swap()

        # ── Step 3: Start LiteLLM ─────────────────────────────────────────
        litellm_pid = self._start_litellm()

        # ── Save PIDs ─────────────────────────────────────────────────────
        state = {
            "llama_swap_pid": swap_pid,
            "litellm_pid":    litellm_pid,
        }
        self._save_state(state)

        log.info("Local stack started", **state)
        return state

    def stop(self) -> None:
        """Stop all managed processes."""
        state = self._load_state()
        if not state:
            log.info("No running stack found (no PID file)")
            return

        for key, pid in state.items():
            if pid:
                self._kill(pid, key)

        _STATE_FILE.unlink(missing_ok=True)
        log.info("Local stack stopped")

    def status(self) -> dict:
        """Return running status of all components."""
        state  = self._load_state() or {}
        report = self._health.full_report()
        return {
            "pids":   state,
            "health": {k: {"running": v.running, "error": v.error}
                       for k, v in report.items()},
        }

    # ── Internal ───────────────────────────────────────────────────────────

    def _start_llama_swap(self) -> Optional[int]:
        """Spawn llama-swap and wait for it to become healthy."""
        binary = self._cfg.llama_swap_binary
        if not binary:
            # Try PATH
            binary = "llama-swap"

        config_path = str(self._output_dir / self._cfg.llama_swap_config)
        if not Path(config_path).exists():
            raise FileNotFoundError(
                f"llama-swap config not found: {config_path}\n"
                "Run 'forge stack config' to generate it."
            )

        cmd = [binary, "--config", config_path]
        log.info("Starting llama-swap", cmd=" ".join(cmd))

        proc = self._spawn(cmd)
        if not self._wait_healthy(
            f"http://127.0.0.1:{self._cfg.llama_swap_port}/health",
            "llama-swap",
        ):
            proc.terminate()
            raise RuntimeError(
                f"llama-swap failed to start within {_STARTUP_TIMEOUT}s. "
                "Check that the binary is correct and the config is valid."
            )
        return proc.pid

    def _start_litellm(self) -> Optional[int]:
        """Spawn LiteLLM and wait for it to become healthy."""
        config_path = str(self._output_dir / self._cfg.litellm_config)
        if not Path(config_path).exists():
            raise FileNotFoundError(
                f"LiteLLM config not found: {config_path}\n"
                "Run 'forge stack config' to generate it."
            )

        cmd = [
            sys.executable, "-m", "litellm",
            "--config", config_path,
            "--port",   str(self._cfg.litellm_port),
            "--host",   "127.0.0.1",
        ]
        log.info("Starting LiteLLM", cmd=" ".join(cmd))

        proc = self._spawn(cmd)
        if not self._wait_healthy(
            f"http://127.0.0.1:{self._cfg.litellm_port}/health",
            "LiteLLM",
        ):
            proc.terminate()
            raise RuntimeError(
                f"LiteLLM failed to start within {_STARTUP_TIMEOUT}s. "
                "Check 'pip install litellm' and the generated config."
            )
        return proc.pid

    def _spawn(self, cmd: list) -> subprocess.Popen:
        """Spawn a subprocess with stdout/stderr captured to log files."""
        log_path = self._output_dir / f"forge_{cmd[0].split(os.sep)[-1]}.log"
        log_fh   = open(log_path, "a")
        return subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32"
                else 0
            ),
        )

    def _wait_healthy(self, url: str, name: str) -> bool:
        """Poll url until 200 or timeout."""
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                _get(url)
                log.info(f"{name} is healthy", url=url)
                return True
            except Exception:
                time.sleep(_POLL_INTERVAL)
        log.error(f"{name} did not become healthy", url=url,
                  timeout=_STARTUP_TIMEOUT)
        return False

    def _kill(self, pid: int, name: str) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, check=False,
                )
            else:
                os.kill(pid, 15)   # SIGTERM
                time.sleep(1)
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)   # SIGKILL if still alive
                except ProcessLookupError:
                    pass
            log.info("Stopped process", name=name, pid=pid)
        except Exception as exc:
            log.warning("Could not stop process", name=name, pid=pid,
                        error=str(exc))

    def _save_state(self, state: dict) -> None:
        _STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    def _load_state(self) -> Optional[dict]:
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
