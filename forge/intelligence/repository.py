"""
FORGE Repository Intelligence Engine
=====================================
Inspired by Loki's architecture philosophy — deep structural understanding
of a codebase, reverse-engineered into an AS-IS prompt that any LLM can
reason about and that FORGE agents can use as context.

What this does
--------------
Given a directory (or a git URL cloned into the workspace), the engine
produces a `RepositoryReport` — a self-contained, LLM-ready representation
of the repository that captures:

  1. Purpose & Domain    — what the repo does, inferred from README +
                           entry-points + module names
  2. Architecture Map    — layer diagram: packages, modules, key classes,
                           public APIs
  3. Dependency Graph    — internal import relationships + external deps
  4. Entry Points        — CLI commands, main() functions, FastAPI/Flask
                           routes, __all__ exports
  5. Pattern Catalogue   — design patterns detected (factory, singleton,
                           strategy, observer, decorator, MVC/MVP, etc.)
  6. Tech Stack          — languages, frameworks, databases, infra
  7. AS-IS Prompt        — the distilled representation an LLM needs to
                           understand and work within this codebase

Comparison mode
---------------
`engine.compare(repo_a_path, repo_b_path)` produces a `RepoComparisonReport`
— same dimensions as AS-IS but side-by-side, highlighting what changed,
what was added, and what was removed.

Integration with FORGE
----------------------
  • ResearchAgent.run() calls `get_repo_engine().analyse(path)` when the
    task description references an existing codebase.
  • The AS-IS prompt is injected into the PlannerAgent's context so it plans
    changes that fit within the existing architecture.
  • `forge intelligence repo scan <path>` for offline inspection.
  • `forge intelligence repo compare <path_a> <path_b>` for diff analysis.

Design principles (same as Loki's)
------------------------------------
  • Pure static analysis — no LLM calls, no network (except optional git clone)
  • Deterministic — same repo always produces the same report hash
  • Fast — < 2s on a 50k-line codebase
  • No external dependencies — stdlib ast, pathlib, re, configparser only

Environment variables
---------------------
  FORGE_RI_MAX_FILES      max Python files to deep-analyse (default: 200)
  FORGE_RI_MAX_LINES      max lines per file before skipping AST (default: 5000)
  FORGE_RI_INCLUDE_TESTS  include test files in analysis (default: false)
"""
from __future__ import annotations

import ast
import configparser
import hashlib
import os
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from forge.telemetry.logger import get_logger

log = get_logger("repo_intelligence")

# ── Config ─────────────────────────────────────────────────────────────────────

_MAX_FILES     = int(os.environ.get("FORGE_RI_MAX_FILES",    "200"))
_MAX_LINES     = int(os.environ.get("FORGE_RI_MAX_LINES",    "5000"))
_INCL_TESTS    = os.environ.get("FORGE_RI_INCLUDE_TESTS", "false").lower() == "true"


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class ModuleInfo:
    """One Python module's structural summary."""
    path:           str
    package:        str
    classes:        List[str]       = field(default_factory=list)
    functions:      List[str]       = field(default_factory=list)
    imports:        List[str]       = field(default_factory=list)   # internal only
    ext_imports:    List[str]       = field(default_factory=list)   # external packages
    exports:        List[str]       = field(default_factory=list)   # __all__
    entry_points:   List[str]       = field(default_factory=list)   # main/cli/routes
    line_count:     int             = 0
    has_tests:      bool            = False
    docstring:      str             = ""


@dataclass
class PatternMatch:
    """A detected design pattern."""
    pattern:     str    # Singleton, Factory, Observer, …
    location:    str    # module.ClassName
    confidence:  float  # 0.0–1.0
    evidence:    str    # what was found


@dataclass
class RepositoryReport:
    """
    Complete AS-IS understanding of one repository.

    Consumed by:
      • ResearchAgent  — injects to_as_is_prompt() into Planner context
      • CLI            — `forge intelligence repo scan`
      • Comparison     — RepoComparisonReport.left / .right
    """
    root_path:       str
    repo_name:       str
    report_hash:     str

    # Structural
    packages:        List[str]         = field(default_factory=list)
    modules:         List[ModuleInfo]  = field(default_factory=list)
    entry_points:    List[str]         = field(default_factory=list)
    public_api:      List[str]         = field(default_factory=list)

    # Purpose & domain
    purpose:         str = ""          # inferred one-liner
    domain_tags:     List[str]  = field(default_factory=list)
    readme_summary:  str = ""

    # Tech stack
    languages:       List[str]  = field(default_factory=list)
    frameworks:      List[str]  = field(default_factory=list)
    databases:       List[str]  = field(default_factory=list)
    infra:           List[str]  = field(default_factory=list)
    ext_deps:        List[str]  = field(default_factory=list)

    # Architecture
    layer_map:       Dict[str, List[str]] = field(default_factory=dict)  # layer → modules
    import_graph:    Dict[str, List[str]] = field(default_factory=dict)  # module → imports
    patterns:        List[PatternMatch]   = field(default_factory=list)

    # Stats
    total_files:     int   = 0
    total_lines:     int   = 0
    total_classes:   int   = 0
    total_functions: int   = 0
    test_files:      int   = 0

    def to_as_is_prompt(self, max_chars: int = 8000) -> str:
        """
        The AS-IS prompt — a self-contained LLM-ready description of this
        repository that a planner agent can use to write code that fits
        within the existing architecture.

        This is the core output: the same concept as Loki's repo reverse-
        engineering, distilled into a prompt that says "here is what this
        codebase IS, here is how it is structured, here is how to extend it."
        """
        parts = [
            f"=== REPOSITORY AS-IS: {self.repo_name} ===",
            "",
        ]

        # Purpose
        if self.purpose:
            parts += [f"PURPOSE: {self.purpose}", ""]

        if self.readme_summary:
            parts += [
                "README SUMMARY:",
                textwrap.fill(self.readme_summary[:600], width=100),
                "",
            ]

        # Tech stack
        stack_parts = []
        if self.frameworks:
            stack_parts.append(f"Frameworks: {', '.join(self.frameworks[:8])}")
        if self.databases:
            stack_parts.append(f"Databases: {', '.join(self.databases[:5])}")
        if self.infra:
            stack_parts.append(f"Infra: {', '.join(self.infra[:5])}")
        if self.ext_deps:
            stack_parts.append(f"Key deps: {', '.join(self.ext_deps[:12])}")
        if stack_parts:
            parts += ["TECH STACK:"] + [f"  {s}" for s in stack_parts] + [""]

        # Architecture layers
        if self.layer_map:
            parts.append("ARCHITECTURE LAYERS:")
            for layer, mods in sorted(self.layer_map.items()):
                parts.append(f"  {layer}: {', '.join(mods[:6])}")
            parts.append("")

        # Entry points
        if self.entry_points:
            parts += [
                "ENTRY POINTS (start here when navigating the codebase):",
                *[f"  • {ep}" for ep in self.entry_points[:10]],
                "",
            ]

        # Public API
        if self.public_api:
            parts += [
                "PUBLIC API (symbols available to external callers):",
                *[f"  • {sym}" for sym in self.public_api[:20]],
                "",
            ]

        # Key modules
        top_modules = sorted(
            self.modules,
            key=lambda m: len(m.classes) + len(m.functions),
            reverse=True,
        )[:15]
        if top_modules:
            parts.append("KEY MODULES:")
            for m in top_modules:
                cls_str = f"  classes: {', '.join(m.classes[:4])}" if m.classes else ""
                fn_str  = f"  funcs: {', '.join(m.functions[:4])}" if m.functions else ""
                parts.append(f"  {m.package}")
                if cls_str:
                    parts.append(cls_str)
                if fn_str:
                    parts.append(fn_str)
            parts.append("")

        # Patterns
        if self.patterns:
            parts += [
                "DESIGN PATTERNS DETECTED:",
                *[
                    f"  • {p.pattern} in {p.location}  [{p.confidence:.0%}] — {p.evidence}"
                    for p in sorted(self.patterns, key=lambda p: p.confidence, reverse=True)[:8]
                ],
                "",
            ]

        # Import graph (top-level only)
        if self.import_graph:
            parts.append("INTERNAL DEPENDENCY HIGHLIGHTS (who imports what):")
            shown = 0
            for src, targets in sorted(self.import_graph.items()):
                if targets and shown < 10:
                    parts.append(f"  {src} → {', '.join(targets[:4])}")
                    shown += 1
            parts.append("")

        # Stats
        parts += [
            "CODEBASE STATS:",
            f"  {self.total_files} files | {self.total_lines:,} lines | "
            f"{self.total_classes} classes | {self.total_functions} functions | "
            f"{self.test_files} test files",
            "",
            "=== END AS-IS ===",
        ]

        result = "\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n[… truncated — full report available via forge intelligence repo scan]"
        return result

    def to_text_report(self) -> str:
        """Full human-readable report for CLI output."""
        lines = [
            f"Repository Intelligence Report — {self.repo_name}",
            "=" * 60,
            f"Root:      {self.root_path}",
            f"Hash:      {self.report_hash}",
            f"Files:     {self.total_files} Python files ({self.total_lines:,} lines)",
            f"Symbols:   {self.total_classes} classes / {self.total_functions} functions",
            f"Tests:     {self.test_files} test files",
            "",
        ]

        if self.purpose:
            lines += [f"Purpose: {self.purpose}", ""]

        if self.readme_summary:
            lines += ["README Summary:", textwrap.fill(self.readme_summary[:400], 80), ""]

        if self.frameworks or self.databases or self.ext_deps:
            lines.append("── Tech Stack ──────────────────────────────────────────")
            if self.frameworks:
                lines.append(f"  Frameworks:  {', '.join(self.frameworks)}")
            if self.databases:
                lines.append(f"  Databases:   {', '.join(self.databases)}")
            if self.infra:
                lines.append(f"  Infra:       {', '.join(self.infra)}")
            if self.ext_deps:
                lines.append(f"  Deps:        {', '.join(self.ext_deps[:15])}")
            lines.append("")

        if self.layer_map:
            lines.append("── Architecture Layers ─────────────────────────────────")
            for layer, mods in sorted(self.layer_map.items()):
                lines.append(f"  {layer:<20} {', '.join(mods)}")
            lines.append("")

        if self.entry_points:
            lines += ["── Entry Points ────────────────────────────────────────"]
            for ep in self.entry_points:
                lines.append(f"  • {ep}")
            lines.append("")

        if self.patterns:
            lines += ["── Design Patterns ─────────────────────────────────────"]
            for p in sorted(self.patterns, key=lambda x: x.confidence, reverse=True):
                lines.append(f"  [{p.confidence:.0%}] {p.pattern:<20} {p.location}")
                lines.append(f"        {p.evidence}")
            lines.append("")

        if self.import_graph:
            lines += ["── Internal Import Graph (top 15) ──────────────────────"]
            shown = 0
            for src, targets in sorted(
                self.import_graph.items(),
                key=lambda kv: len(kv[1]),
                reverse=True,
            ):
                if shown >= 15:
                    break
                if targets:
                    lines.append(f"  {src} → {', '.join(targets[:5])}")
                    shown += 1
            lines.append("")

        return "\n".join(lines)

    def summary_dict(self) -> dict:
        return {
            "repo_name":       self.repo_name,
            "total_files":     self.total_files,
            "total_lines":     self.total_lines,
            "total_classes":   self.total_classes,
            "total_functions": self.total_functions,
            "packages":        self.packages[:10],
            "frameworks":      self.frameworks,
            "entry_points":    self.entry_points[:5],
            "patterns":        [p.pattern for p in self.patterns[:5]],
            "report_hash":     self.report_hash,
        }


@dataclass
class RepoComparisonReport:
    """Side-by-side comparison of two repositories."""
    left:  RepositoryReport
    right: RepositoryReport

    def to_diff_prompt(self) -> str:
        """
        Produces an AS-IS diff prompt: what changed, what was added,
        what was removed — in LLM-ready form.
        """
        parts = [
            f"=== REPOSITORY COMPARISON: {self.left.repo_name} vs {self.right.repo_name} ===",
            "",
        ]

        # Framework/stack changes
        added_fw   = set(self.right.frameworks) - set(self.left.frameworks)
        removed_fw = set(self.left.frameworks) - set(self.right.frameworks)
        if added_fw:
            parts.append(f"FRAMEWORKS ADDED:   {', '.join(sorted(added_fw))}")
        if removed_fw:
            parts.append(f"FRAMEWORKS REMOVED: {', '.join(sorted(removed_fw))}")
        if added_fw or removed_fw:
            parts.append("")

        # Package changes
        left_pkgs  = set(self.left.packages)
        right_pkgs = set(self.right.packages)
        added_pkgs   = right_pkgs - left_pkgs
        removed_pkgs = left_pkgs - right_pkgs
        if added_pkgs:
            parts.append(f"PACKAGES ADDED:   {', '.join(sorted(added_pkgs))}")
        if removed_pkgs:
            parts.append(f"PACKAGES REMOVED: {', '.join(sorted(removed_pkgs))}")
        if added_pkgs or removed_pkgs:
            parts.append("")

        # Pattern changes
        left_patterns  = {p.pattern for p in self.left.patterns}
        right_patterns = {p.pattern for p in self.right.patterns}
        new_patterns   = right_patterns - left_patterns
        lost_patterns  = left_patterns - right_patterns
        if new_patterns:
            parts.append(f"NEW PATTERNS:     {', '.join(sorted(new_patterns))}")
        if lost_patterns:
            parts.append(f"REMOVED PATTERNS: {', '.join(sorted(lost_patterns))}")

        # Stats delta
        dl = self.right.total_lines - self.left.total_lines
        dc = self.right.total_classes - self.left.total_classes
        df = self.right.total_functions - self.left.total_functions
        parts += [
            "",
            f"SIZE DELTA: lines {dl:+,} | classes {dc:+} | functions {df:+}",
            "",
        ]

        # AS-IS for both
        parts += [
            "── LEFT AS-IS ──────────────────────────────────────────────",
            self.left.to_as_is_prompt(max_chars=3000),
            "",
            "── RIGHT AS-IS ─────────────────────────────────────────────",
            self.right.to_as_is_prompt(max_chars=3000),
            "",
            "=== END COMPARISON ===",
        ]

        return "\n".join(parts)

    def to_text_report(self) -> str:
        return self.to_diff_prompt()


# ── Detection helpers ──────────────────────────────────────────────────────────

# Framework signals: (import_name_fragment, display_name)
_FRAMEWORK_SIGNALS: List[Tuple[str, str]] = [
    ("fastapi",      "FastAPI"),
    ("flask",        "Flask"),
    ("django",       "Django"),
    ("starlette",    "Starlette"),
    ("tornado",      "Tornado"),
    ("sanic",        "Sanic"),
    ("aiohttp",      "aiohttp"),
    ("uvicorn",      "uvicorn"),
    ("celery",       "Celery"),
    ("sqlalchemy",   "SQLAlchemy"),
    ("alembic",      "Alembic"),
    ("pydantic",     "Pydantic"),
    ("typer",        "Typer"),
    ("click",        "Click"),
    ("pytest",       "pytest"),
    ("asyncio",      "asyncio"),
    ("anthropic",    "Anthropic SDK"),
    ("openai",       "OpenAI SDK"),
    ("langchain",    "LangChain"),
    ("transformers", "HuggingFace Transformers"),
    ("torch",        "PyTorch"),
    ("tensorflow",   "TensorFlow"),
    ("sklearn",      "scikit-learn"),
    ("numpy",        "NumPy"),
    ("pandas",       "pandas"),
    ("chromadb",     "ChromaDB"),
    ("redis",        "Redis"),
    ("grpc",         "gRPC"),
    ("kafka",        "Kafka"),
    ("boto3",        "AWS Boto3"),
    ("kubernetes",   "Kubernetes client"),
    ("docker",       "Docker SDK"),
]

_DB_SIGNALS: List[Tuple[str, str]] = [
    ("sqlite3",      "SQLite"),
    ("psycopg2",     "PostgreSQL"),
    ("asyncpg",      "PostgreSQL (async)"),
    ("pymongo",      "MongoDB"),
    ("motor",        "MongoDB (async)"),
    ("redis",        "Redis"),
    ("elasticsearch","Elasticsearch"),
    ("chromadb",     "ChromaDB"),
    ("pinecone",     "Pinecone"),
    ("weaviate",     "Weaviate"),
]

_INFRA_SIGNALS: List[Tuple[str, str]] = [
    ("docker",       "Docker"),
    ("kubernetes",   "Kubernetes"),
    ("boto3",        "AWS"),
    ("azure",        "Azure"),
    ("google.cloud", "GCP"),
    ("prometheus",   "Prometheus"),
    ("opentelemetry","OpenTelemetry"),
    ("sentry",       "Sentry"),
]

# Pattern detection: (pattern_name, evidence_function)
def _detect_singleton(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Singleton: class with _instance class var + classmethod getInstance."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        has_instance_var = any(
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and ("_instance" in t.id or "_singleton" in t.id)
                for t in (n.targets if hasattr(n, "targets") else [])
            )
            for n in ast.walk(node)
        )
        has_classmethod = any(
            isinstance(n, ast.FunctionDef)
            and any(
                isinstance(d, ast.Name) and d.id in ("classmethod", "staticmethod")
                for d in n.decorator_list
            )
            for n in node.body
        )
        if has_instance_var and has_classmethod:
            return PatternMatch(
                pattern="Singleton",
                location=f"{module_path}.{node.name}",
                confidence=0.80,
                evidence=f"class {node.name} has _instance var + classmethod",
            )
    return None


def _detect_factory(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Factory: function/method named create_* or *_factory returning objects."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            name = node.name.lower()
            if "factory" in name or name.startswith("create_") or name.startswith("make_"):
                return PatternMatch(
                    pattern="Factory",
                    location=f"{module_path}.{node.name}",
                    confidence=0.75,
                    evidence=f"function {node.name} matches factory naming pattern",
                )
    return None


def _detect_observer(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Observer: subscribe/on_event/emit/notify methods."""
    _OBSERVER_METHODS = {"subscribe", "unsubscribe", "notify", "emit", "on_event",
                         "add_listener", "remove_listener", "dispatch", "publish"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            method_names = {
                n.name.lower()
                for n in node.body
                if isinstance(n, ast.FunctionDef)
            }
            matched = method_names & _OBSERVER_METHODS
            if len(matched) >= 2:
                return PatternMatch(
                    pattern="Observer/Event",
                    location=f"{module_path}.{node.name}",
                    confidence=0.72,
                    evidence=f"class {node.name} has methods: {', '.join(sorted(matched))}",
                )
    return None


def _detect_decorator_pattern(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Decorator pattern: class wrapping another of the same interface."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Look for __init__ that accepts another object of same type or interface
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    if any("wrapped" in a or "component" in a or "inner" in a
                           for a in args):
                        return PatternMatch(
                            pattern="Decorator",
                            location=f"{module_path}.{node.name}",
                            confidence=0.68,
                            evidence=f"__init__ wraps component: args={args}",
                        )
    return None


def _detect_registry(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Registry pattern: class with register/get/list methods and a dict store."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            if ("register" in methods or "register_tool" in methods) and \
               ("get" in methods or "list_tools" in methods or "list" in methods):
                return PatternMatch(
                    pattern="Registry",
                    location=f"{module_path}.{node.name}",
                    confidence=0.80,
                    evidence=f"class {node.name} has register + get/list methods",
                )
    return None


def _detect_strategy(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Strategy: ABC/abstract base class with a single abstract execute/run method."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            abstract_methods = []
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    if any(
                        (isinstance(d, ast.Name) and d.id == "abstractmethod") or
                        (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
                        for d in item.decorator_list
                    ):
                        abstract_methods.append(item.name)
            if abstract_methods:
                return PatternMatch(
                    pattern="Strategy/Abstract",
                    location=f"{module_path}.{node.name}",
                    confidence=0.78,
                    evidence=f"ABC with abstract: {', '.join(abstract_methods[:3])}",
                )
    return None


def _detect_pipeline(tree: ast.AST, module_path: str) -> Optional[PatternMatch]:
    """Detect Pipeline: functions chained via >> or | operators, or named step_N."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            name = node.name.lower()
            if re.search(r"step_?\d|stage_?\d|pipe", name):
                return PatternMatch(
                    pattern="Pipeline",
                    location=f"{module_path}.{node.name}",
                    confidence=0.65,
                    evidence=f"function {node.name} matches pipeline naming",
                )
    return None


_PATTERN_DETECTORS = [
    _detect_singleton,
    _detect_factory,
    _detect_observer,
    _detect_decorator_pattern,
    _detect_registry,
    _detect_strategy,
    _detect_pipeline,
]


# ── Architecture layer inference ───────────────────────────────────────────────

_LAYER_SIGNALS: Dict[str, List[str]] = {
    "api / routes":      ["api", "routes", "router", "endpoint", "handler", "view", "controller"],
    "agents":            ["agent", "executor", "runner", "orchestrat"],
    "llm / providers":   ["llm", "provider", "client", "model", "completion"],
    "tools":             ["tool", "plugin", "extension", "capability"],
    "memory / storage":  ["memory", "store", "storage", "cache", "db", "repository", "repo"],
    "config":            ["config", "settings", "env", "configuration"],
    "telemetry":         ["log", "metric", "trace", "observ", "monitor"],
    "auth / security":   ["auth", "security", "permission", "token", "jwt", "crypto"],
    "tests":             ["test", "spec", "fixture", "mock"],
    "utils / helpers":   ["util", "helper", "common", "shared", "base", "mixin"],
}


def _infer_layer(package_path: str) -> str:
    """Map a package path to its architecture layer."""
    lower = package_path.lower().replace("/", ".").replace("\\", ".")
    for layer, signals in _LAYER_SIGNALS.items():
        if any(sig in lower for sig in signals):
            return layer
    return "core"


# ── Domain/purpose inference ───────────────────────────────────────────────────

_DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "AI/ML":        ["llm", "openai", "anthropic", "transformer", "model", "inference",
                     "embedding", "vector", "agent", "langchain"],
    "web API":      ["fastapi", "flask", "django", "route", "endpoint", "http", "rest"],
    "data":         ["pandas", "numpy", "spark", "etl", "pipeline", "dataframe"],
    "DevOps":       ["docker", "kubernetes", "deploy", "ci", "terraform", "ansible"],
    "CLI tool":     ["click", "typer", "argparse", "console", "terminal"],
    "security":     ["crypto", "auth", "jwt", "oauth", "permission", "vault"],
    "testing":      ["pytest", "unittest", "mock", "fixture"],
    "database":     ["sqlalchemy", "orm", "migration", "alembic", "query"],
    "messaging":    ["kafka", "rabbitmq", "pubsub", "queue", "event", "stream"],
}


# ── Main engine ────────────────────────────────────────────────────────────────

class RepositoryIntelligenceEngine:
    """
    Reverse-engineers a repository into an AS-IS prompt.

    Thread-safe: each analyse() call is independent.
    """

    def analyse(
        self,
        root_path: str,
        repo_name: Optional[str] = None,
    ) -> RepositoryReport:
        """
        Full repository analysis.

        Args:
            root_path:  Path to the repository root directory.
            repo_name:  Override the repo name (defaults to directory basename).

        Returns:
            RepositoryReport with AS-IS prompt, architecture map, patterns, etc.
        """
        root = Path(root_path).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Repository path not found: {root_path}")

        name = repo_name or root.name
        log.info("Repository intelligence: starting", repo=name, path=str(root))

        report = RepositoryReport(
            root_path   = str(root),
            repo_name   = name,
            report_hash = "",
        )

        # ── Collect Python files ──────────────────────────────────────────
        py_files = self._collect_py_files(root)
        report.total_files = len(py_files)
        report.test_files  = sum(1 for f in py_files if self._is_test_file(f))

        # ── Read README ───────────────────────────────────────────────────
        report.readme_summary = self._read_readme(root)

        # ── Tech stack from requirements / pyproject / setup.cfg ─────────
        ext_deps = self._collect_ext_deps(root)
        report.ext_deps = sorted(ext_deps)[:30]
        report.frameworks, report.databases, report.infra = \
            self._classify_deps(ext_deps)

        # ── Analyse each module ───────────────────────────────────────────
        all_imports: Set[str] = set()
        for py_file in py_files[:_MAX_FILES]:
            mod = self._analyse_module(py_file, root)
            if mod:
                report.modules.append(mod)
                report.total_lines    += mod.line_count
                report.total_classes  += len(mod.classes)
                report.total_functions += len(mod.functions)
                all_imports.update(mod.ext_imports)

        # Augment frameworks/databases from actual imports found in code
        for imp in all_imports:
            for sig, display in _FRAMEWORK_SIGNALS:
                if sig in imp and display not in report.frameworks:
                    report.frameworks.append(display)
            for sig, display in _DB_SIGNALS:
                if sig in imp and display not in report.databases:
                    report.databases.append(display)
            for sig, display in _INFRA_SIGNALS:
                if sig in imp and display not in report.infra:
                    report.infra.append(display)

        # ── Packages ─────────────────────────────────────────────────────
        report.packages = sorted({
            m.package.split(".")[0]
            for m in report.modules
            if m.package
        })

        # ── Architecture layer map ────────────────────────────────────────
        layer_map: Dict[str, List[str]] = defaultdict(list)
        for m in report.modules:
            layer = _infer_layer(m.package)
            if m.package not in layer_map[layer]:
                layer_map[layer].append(m.package)
        report.layer_map = dict(layer_map)

        # ── Internal import graph ─────────────────────────────────────────
        pkg_set = set(report.packages)
        for m in report.modules:
            internal = [
                i for i in m.imports
                if any(i.startswith(p) for p in pkg_set)
            ]
            if internal:
                report.import_graph[m.package] = internal[:8]

        # ── Entry points ──────────────────────────────────────────────────
        report.entry_points = self._find_entry_points(report.modules, root)

        # ── Public API ────────────────────────────────────────────────────
        report.public_api = self._collect_public_api(report.modules)

        # ── Design patterns ───────────────────────────────────────────────
        report.patterns = self._detect_patterns(py_files, root)

        # ── Purpose / domain ─────────────────────────────────────────────
        report.purpose, report.domain_tags = self._infer_purpose(
            report.readme_summary,
            report.frameworks,
            report.packages,
            list(all_imports),
        )

        # ── Report hash (stable) ──────────────────────────────────────────
        hash_src = (
            f"{report.total_files}{report.total_lines}"
            f"{'|'.join(sorted(report.packages))}"
        )
        report.report_hash = hashlib.md5(
            hash_src.encode("utf-8")
        ).hexdigest()[:12]

        log.info(
            "Repository intelligence: complete",
            repo=name,
            files=report.total_files,
            lines=report.total_lines,
            packages=len(report.packages),
            patterns=len(report.patterns),
        )
        return report

    def compare(
        self,
        path_a: str,
        path_b: str,
        name_a: Optional[str] = None,
        name_b: Optional[str] = None,
    ) -> RepoComparisonReport:
        """
        Analyse two repositories and produce a side-by-side comparison.
        """
        left  = self.analyse(path_a, repo_name=name_a)
        right = self.analyse(path_b, repo_name=name_b)
        return RepoComparisonReport(left=left, right=right)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _collect_py_files(self, root: Path) -> List[Path]:
        EXCLUDE_DIRS = {
            ".git", ".venv", "venv", "__pycache__", ".mypy_cache",
            ".pytest_cache", "node_modules", "dist", "build", "eggs",
            ".eggs", "*.egg-info",
        }
        files = []
        for f in sorted(root.rglob("*.py")):
            if any(part in EXCLUDE_DIRS or part.endswith(".egg-info")
                   for part in f.parts):
                continue
            if not _INCL_TESTS and self._is_test_file(f):
                continue
            files.append(f)
        return files

    def _is_test_file(self, f: Path) -> bool:
        return f.name.startswith("test_") or f.name.endswith("_test.py") \
               or "tests" in f.parts or "test" in f.parts

    def _read_readme(self, root: Path) -> str:
        for name in ("README.md", "README.rst", "README.txt", "README"):
            p = root / name
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    # Strip markdown headers/badges for a cleaner summary
                    lines = [
                        l for l in text.splitlines()
                        if l.strip()
                        and not l.startswith("[![")
                        and not l.startswith("![")
                    ]
                    return " ".join(lines[:30])[:800]
                except OSError:
                    pass
        return ""

    def _collect_ext_deps(self, root: Path) -> Set[str]:
        deps: Set[str] = set()

        # requirements.txt / requirements*.txt
        for req_file in root.glob("requirements*.txt"):
            try:
                for line in req_file.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pkg = re.split(r"[>=<!;\[\s]", line)[0].lower()
                        if pkg:
                            deps.add(pkg)
            except OSError:
                pass

        # pyproject.toml (simple regex — no toml parser required)
        pyproj = root / "pyproject.toml"
        if pyproj.exists():
            try:
                text = pyproj.read_text()
                for m in re.finditer(
                    r'["\']([a-z][a-z0-9_\-]+)["\']', text
                ):
                    pkg = m.group(1).lower()
                    if len(pkg) > 2:
                        deps.add(pkg)
            except OSError:
                pass

        # setup.cfg
        setup_cfg = root / "setup.cfg"
        if setup_cfg.exists():
            try:
                cfg = configparser.ConfigParser()
                cfg.read(str(setup_cfg))
                for key in ("install_requires", "tests_require"):
                    raw = cfg.get("options", key, fallback="")
                    for line in raw.splitlines():
                        pkg = re.split(r"[>=<!;\[\s]", line.strip())[0].lower()
                        if pkg:
                            deps.add(pkg)
            except Exception:
                pass

        return deps

    def _classify_deps(
        self, deps: Set[str]
    ) -> Tuple[List[str], List[str], List[str]]:
        frameworks, databases, infra = [], [], []
        for dep in deps:
            for sig, display in _FRAMEWORK_SIGNALS:
                if sig in dep and display not in frameworks:
                    frameworks.append(display)
            for sig, display in _DB_SIGNALS:
                if sig in dep and display not in databases:
                    databases.append(display)
            for sig, display in _INFRA_SIGNALS:
                if sig in dep and display not in infra:
                    infra.append(display)
        return frameworks, databases, infra

    def _analyse_module(self, py_file: Path, root: Path) -> Optional[ModuleInfo]:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = source.splitlines()
        line_count = len(lines)

        # Derive dot-notation package name
        try:
            rel = py_file.relative_to(root)
            package = ".".join(rel.with_suffix("").parts)
        except ValueError:
            package = py_file.stem

        mod = ModuleInfo(
            path       = str(py_file),
            package    = package,
            line_count = line_count,
            has_tests  = self._is_test_file(py_file),
        )

        if line_count > _MAX_LINES:
            # Too large for AST — extract imports via regex only
            for m in re.finditer(
                r"^(?:from|import)\s+([\w.]+)", source, re.MULTILINE
            ):
                mod.imports.append(m.group(1))
            return mod

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            return mod

        # Module docstring
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            mod.docstring = tree.body[0].value.value[:200]

        # Classes and functions
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                mod.classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only top-level functions (depth check via parent — heuristic)
                if node.col_offset == 0:
                    mod.functions.append(node.name)

        # Imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    mod.ext_imports.append(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    mod.ext_imports.append(top)
                    if node.level == 0:   # absolute import
                        mod.imports.append(node.module)

        # __all__ exports
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "__all__"
                    for t in node.targets
                )
            ):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            mod.exports.append(elt.value)

        # Entry-point detection
        mod.entry_points = self._detect_entry_points(tree, package)

        return mod

    def _detect_entry_points(self, tree: ast.AST, package: str) -> List[str]:
        eps = []
        for node in tree.body:
            # if __name__ == "__main__"
            if isinstance(node, ast.If):
                test = node.test
                if (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                ):
                    eps.append(f"{package}  [__main__]")

            # @app.route / @router.get etc.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    dec_str = ast.dump(dec)
                    if any(k in dec_str for k in
                           ("route", "get", "post", "put", "delete", "patch",
                            "command", "cli", "task")):
                        eps.append(f"{package}.{node.name}  [route/cmd]")
                        break

        return eps

    def _find_entry_points(
        self, modules: List[ModuleInfo], root: Path
    ) -> List[str]:
        eps = []
        for m in modules:
            eps.extend(m.entry_points)

        # CLI entry points from pyproject.toml / setup.cfg
        pyproj = root / "pyproject.toml"
        if pyproj.exists():
            try:
                text = pyproj.read_text()
                for m in re.finditer(
                    r"\[tool\.poetry\.scripts\].*?\n(.*?)(?:\n\[|\Z)",
                    text, re.DOTALL
                ):
                    for line in m.group(1).splitlines():
                        if "=" in line:
                            eps.append(f"CLI: {line.strip()}")
            except OSError:
                pass

        return eps[:20]

    def _collect_public_api(self, modules: List[ModuleInfo]) -> List[str]:
        api = []
        for m in modules:
            if m.exports:
                api.extend(f"{m.package}.{sym}" for sym in m.exports[:5])
            elif "api" in m.package.lower() or "public" in m.package.lower():
                api.extend(f"{m.package}.{cls}" for cls in m.classes[:3])
                api.extend(f"{m.package}.{fn}"  for fn  in m.functions[:3])
        return api[:30]

    def _detect_patterns(
        self, py_files: List[Path], root: Path
    ) -> List[PatternMatch]:
        matches = []
        seen: Set[str] = set()

        for py_file in py_files[:_MAX_FILES]:
            if py_file.stat().st_size > 200_000:
                continue
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                if len(source.splitlines()) > _MAX_LINES:
                    continue
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue

            try:
                pkg = ".".join(
                    py_file.relative_to(root).with_suffix("").parts
                )
            except ValueError:
                pkg = py_file.stem

            for detector in _PATTERN_DETECTORS:
                try:
                    result = detector(tree, pkg)
                    if result and result.location not in seen:
                        matches.append(result)
                        seen.add(result.location)
                except Exception:
                    pass

        return sorted(matches, key=lambda p: p.confidence, reverse=True)

    def _infer_purpose(
        self,
        readme: str,
        frameworks: List[str],
        packages: List[str],
        imports: List[str],
    ) -> Tuple[str, List[str]]:
        """Infer a one-line purpose statement and domain tags."""
        all_text = (
            readme.lower() + " "
            + " ".join(f.lower() for f in frameworks) + " "
            + " ".join(packages) + " "
            + " ".join(imports)
        )

        domain_tags = []
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            if sum(1 for kw in keywords if kw in all_text) >= 2:
                domain_tags.append(domain)

        # Extract first non-badge sentence from README as purpose
        purpose = ""
        for sentence in re.split(r"[.!?]", readme):
            clean = sentence.strip()
            if len(clean) > 20 and not clean.startswith("[") and not clean.startswith("#"):
                purpose = clean[:120]
                break

        if not purpose and domain_tags:
            purpose = f"{' / '.join(domain_tags[:2])} framework"

        return purpose, domain_tags[:5]


# ── Module-level singleton ─────────────────────────────────────────────────────

_repo_engine: Optional[RepositoryIntelligenceEngine] = None


def get_repo_engine() -> RepositoryIntelligenceEngine:
    global _repo_engine
    if _repo_engine is None:
        _repo_engine = RepositoryIntelligenceEngine()
    return _repo_engine
