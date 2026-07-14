"""
FORGE Thunderbird — Model Registry
====================================
Scans model directories for GGUF files, infers roles from filenames,
associates mmproj files with their vision models, and builds
ModelProfile objects used by the config generator and router.

Role detection heuristics (applied in order, first match wins):
  mmproj    → vision-projection (not a standalone model — attached to vision)
  VL / vl   → vision
  deepseek  → reasoning
  qwen3     → coding  (qwen3 without VL suffix)
  r1 / R1   → reasoning
  custom    → fallback for anything not matched

Users can override auto-detected roles by setting:
  FORGE_CODING_MODEL_PATH, FORGE_REASONING_MODEL_PATH, FORGE_VISION_MODEL_PATH
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from forge.telemetry.logger import get_logger

log = get_logger("local_stack.registry")


# ── Role detection patterns (order matters — first match wins) ─────────────────

_ROLE_PATTERNS: List[Tuple[str, str]] = [
    # mmproj — never a standalone model
    (r"mmproj",                               "mmproj"),
    # Vision multimodal
    (r"(?i)VL|vision|multimodal|vlm",         "vision"),
    # Reasoning / deep thinking
    (r"(?i)deepseek|r1|reasoning|think",      "reasoning"),
    # Coding / general
    (r"(?i)qwen|codellama|starcoder|coding",  "coding"),
    # Fallback
    (r".*",                                   "custom"),
]

# Alias derived from role (used as llama-swap model name)
_ROLE_TO_DEFAULT_ALIAS: Dict[str, str] = {
    "coding":    "qwen3",
    "reasoning": "deepseek",
    "vision":    "qwen3vl",
    "custom":    "custom",
}

# Human-readable descriptions shown in `forge stack models`
_ROLE_DESCRIPTIONS: Dict[str, str] = {
    "coding":    "Code generation, refactoring, documentation",
    "reasoning": "Architecture, planning, mathematics, multi-step reasoning",
    "vision":    "Image understanding, visual Q&A",
    "custom":    "Custom / unrecognised model",
}


@dataclass
class GGUFFile:
    """One discovered .gguf file."""
    path:      Path
    role:      str          # coding | reasoning | vision | mmproj | custom
    alias:     str          # llama-swap alias
    size_gb:   float        # file size in GB
    filename:  str          # basename without extension


@dataclass
class ModelProfile:
    """
    Complete specification for one loadable model.
    Mirrors forge.config.ModelProfile but self-contained
    so model_registry.py has no circular import from config.
    """
    role:         str
    alias:        str
    model_path:   str
    mmproj_path:  str  = ""
    context_len:  int  = 4096
    gpu_layers:   int  = 99
    threads:      int  = 8
    description:  str  = ""
    size_gb:      float = 0.0

    def to_dict(self) -> dict:
        return {
            "role":        self.role,
            "alias":       self.alias,
            "model_path":  self.model_path,
            "mmproj_path": self.mmproj_path,
            "context_len": self.context_len,
            "gpu_layers":  self.gpu_layers,
            "threads":     self.threads,
            "description": self.description,
            "size_gb":     round(self.size_gb, 2),
        }


class ModelRegistry:
    """
    Discovers and catalogues GGUF models from one or more directories.

    Usage::

        registry = ModelRegistry()
        registry.scan(["D:/models", "E:/models-IMP"])
        profiles = registry.profiles   # dict: role → ModelProfile
        print(registry.summary())
    """

    def __init__(
        self,
        gpu_layers: int = 99,
        threads:    int = 8,
        context_len:int = 4096,
    ):
        self._gpu_layers  = gpu_layers
        self._threads     = threads
        self._context_len = context_len
        self._gguf_files: List[GGUFFile] = []
        self.profiles:    Dict[str, ModelProfile] = {}  # role → ModelProfile

    # ── Public API ─────────────────────────────────────────────────────────

    def scan(self, dirs: List[str]) -> Dict[str, ModelProfile]:
        """
        Scan directories recursively for *.gguf files, infer roles,
        associate mmproj files, and build ModelProfile objects.

        Returns the profiles dict (also stored as self.profiles).
        """
        self._gguf_files = []
        self.profiles    = {}

        for d in dirs:
            root = Path(d)
            if not root.exists():
                log.warning("Model directory not found, skipping", path=d)
                continue
            for path in sorted(root.rglob("*.gguf")):
                self._gguf_files.append(self._parse_gguf(path))
                log.info("Discovered GGUF", path=str(path),
                         role=self._gguf_files[-1].role)

        # Separate mmproj files from loadable models
        mmproj_files = [f for f in self._gguf_files if f.role == "mmproj"]
        model_files  = [f for f in self._gguf_files if f.role != "mmproj"]

        # Build one profile per role (first discovered wins per role)
        for gguf in model_files:
            if gguf.role in self.profiles:
                log.info(
                    "Skipping duplicate role (already have one)",
                    role=gguf.role, path=str(gguf.path),
                )
                continue

            # Find an mmproj companion for vision models
            mmproj_path = ""
            if gguf.role == "vision" and mmproj_files:
                mmproj_path = str(self._match_mmproj(gguf, mmproj_files))

            self.profiles[gguf.role] = ModelProfile(
                role         = gguf.role,
                alias        = gguf.alias,
                model_path   = str(gguf.path),
                mmproj_path  = mmproj_path,
                context_len  = self._context_len,
                gpu_layers   = self._gpu_layers,
                threads      = self._threads,
                description  = _ROLE_DESCRIPTIONS.get(gguf.role, ""),
                size_gb      = gguf.size_gb,
            )

        log.info(
            "Model scan complete",
            dirs=dirs,
            found=len(self._gguf_files),
            profiles=list(self.profiles.keys()),
        )
        return self.profiles

    def get_profile(self, role: str) -> Optional[ModelProfile]:
        """Return the profile for a role, or None if not found."""
        return self.profiles.get(role)

    def get_alias(self, role: str) -> str:
        """Return the llama-swap alias for a role."""
        p = self.profiles.get(role)
        return p.alias if p else _ROLE_TO_DEFAULT_ALIAS.get(role, role)

    def all_gguf_files(self) -> List[GGUFFile]:
        return list(self._gguf_files)

    def summary(self) -> str:
        """Human-readable summary for `forge stack models`."""
        if not self._gguf_files:
            return "No GGUF files found. Check FORGE_MODELS_DIR in .env"
        lines = [f"  {'Role':<12} {'Alias':<12} {'Size':>7}  {'Path'}"]
        lines.append("  " + "─" * 72)
        for f in sorted(self._gguf_files, key=lambda x: x.role):
            tag = " [mmproj]" if f.role == "mmproj" else ""
            lines.append(
                f"  {f.role:<12} {f.alias:<12} {f.size_gb:>5.1f}GB  "
                f"{f.path}{tag}"
            )
        return "\n".join(lines)

    def apply_env_overrides(self) -> None:
        """
        Allow per-role path overrides via environment variables.
        These take precedence over auto-discovered files.

          FORGE_CODING_MODEL_PATH    — full path to coding GGUF
          FORGE_REASONING_MODEL_PATH — full path to reasoning GGUF
          FORGE_VISION_MODEL_PATH    — full path to vision GGUF
          FORGE_MMPROJ_PATH          — full path to mmproj file
        """
        overrides = {
            "coding":    os.environ.get("FORGE_CODING_MODEL_PATH",    ""),
            "reasoning": os.environ.get("FORGE_REASONING_MODEL_PATH", ""),
            "vision":    os.environ.get("FORGE_VISION_MODEL_PATH",    ""),
        }
        mmproj_override = os.environ.get("FORGE_MMPROJ_PATH", "")

        for role, path in overrides.items():
            if not path:
                continue
            p = Path(path)
            if not p.exists():
                log.warning("Override path not found", role=role, path=path)
                continue
            existing = self.profiles.get(role)
            alias    = existing.alias if existing else _ROLE_TO_DEFAULT_ALIAS.get(role, role)
            mmproj   = mmproj_override if role == "vision" else (
                existing.mmproj_path if existing else ""
            )
            self.profiles[role] = ModelProfile(
                role        = role,
                alias       = alias,
                model_path  = path,
                mmproj_path = mmproj,
                context_len = self._context_len,
                gpu_layers  = self._gpu_layers,
                threads     = self._threads,
                description = _ROLE_DESCRIPTIONS.get(role, ""),
                size_gb     = p.stat().st_size / (1024 ** 3),
            )
            log.info("Applied env override", role=role, path=path)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _parse_gguf(self, path: Path) -> GGUFFile:
        stem = path.stem
        role = self._detect_role(stem)
        # Build alias from role; use stem words as fallback for custom
        alias = _ROLE_TO_DEFAULT_ALIAS.get(role, stem.split("-")[0].lower())
        try:
            size_gb = path.stat().st_size / (1024 ** 3)
        except OSError:
            size_gb = 0.0
        return GGUFFile(
            path     = path,
            role     = role,
            alias    = alias,
            size_gb  = size_gb,
            filename = stem,
        )

    def _detect_role(self, stem: str) -> str:
        for pattern, role in _ROLE_PATTERNS:
            if re.search(pattern, stem):
                return role
        return "custom"

    def _match_mmproj(self, vision: GGUFFile, mmprojs: List[GGUFFile]) -> Path:
        """
        Find the best mmproj match for a vision model.
        Prefers an mmproj in the same directory; falls back to first found.
        """
        same_dir = [m for m in mmprojs if m.path.parent == vision.path.parent]
        if same_dir:
            return same_dir[0].path
        return mmprojs[0].path


# ── Module-level singleton ─────────────────────────────────────────────────────

_registry: Optional[ModelRegistry] = None


def get_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        from forge.config import get_config
        cfg = get_config()
        ls  = cfg.local_stack
        _registry = ModelRegistry(
            gpu_layers  = ls.gpu_layers,
            threads     = ls.threads,
            context_len = 4096,
        )
        if ls.models_dirs:
            _registry.scan(ls.models_dirs)
            _registry.apply_env_overrides()
    return _registry
