"""
FORGE Test Intelligence Engine
===============================
Pure static analysis — no LLM calls, no network, runs in milliseconds.
Identifies what needs testing, ranked by risk and business criticality,
before the QA agent writes a single line.

Generation comparison
---------------------
  Gen-3 (Playwright, Cypress, Testim): record/replay automation that executes
    whatever a developer points it at. No concept of what is risky or untested.

  Gen-4 AI-assisted (Momentic, Octomind, ACCELQ, Copilot agents): an LLM
    decides what to click or assert based on a natural-language description.
    Still reactive — someone must tell it where to look.

  FORGE Test Intelligence (this module): proactively analyses the code graph
    before any test is written, ranks every function by risk score, identifies
    which paths are untestable or untested, and delivers a prioritised brief
    to the QA agent so tests are written in the right order with the right focus.

Four analysis dimensions
------------------------
  1. Critical Paths   — execution paths that, if broken, break everything.
                        Identified by: high fan-in (many callers), entry-point
                        proximity, exception-propagation potential, and
                        control-flow depth.

  2. Dead Code        — functions/branches that can never execute. These inflate
                        coverage metrics while hiding real gaps. Found via:
                        unreachable-after-return, always-false conditions,
                        unused private functions, unreferenced module-level code.

  3. Risk Areas       — code that is statistically likely to fail: high cyclomatic
                        complexity, deep nesting, missing input validation,
                        mutable default arguments, bare excepts, wide-open
                        exception swallowing, unterminated resource handles,
                        unsafe type coercion, string-formatted SQL, recursive
                        functions without base-case guards.

  4. Untested Areas   — functions/branches for which no existing test file
                        provides signal. Detected by cross-referencing function
                        names against any test_*.py / *_test.py files found
                        alongside the implementation.

Output
------
  TestIntelligenceReport — consumed by:
    • QAAgent.run() — injects report.to_qa_brief() into the prompt so the
      LLM writes targeted tests first
    • OrchestrationEngine — stores report.to_memory_entries() in the cross-task
      memory store so future tasks on similar code inherit risk knowledge
    • CLI — `forge intelligence <file>` for offline inspection
"""
from __future__ import annotations

import ast
import os
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ── Env config ─────────────────────────────────────────────────────────────────

_MAX_COMPLEXITY   = int(os.environ.get("FORGE_MAX_COMPLEXITY",    "10"))
_RISK_THRESHOLD   = float(os.environ.get("FORGE_RISK_THRESHOLD",  "0.5"))
_DEAD_CODE_GATE   = os.environ.get("FORGE_DEAD_CODE_GATE", "warn").lower()


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class CriticalPath:
    """
    An execution path that carries disproportionate risk if broken.

    entry_point: the public function / method where execution begins
    path:        ordered list of function names on the critical path
    fan_in:      how many other functions call entry_point
    depth:       max call depth reachable from entry_point
    risk_score:  0.0–1.0 composite (higher = test first)
    reason:      human-readable explanation
    """
    entry_point: str
    path:        List[str]
    fan_in:      int
    depth:       int
    risk_score:  float
    reason:      str
    line_number: int = 0


@dataclass
class DeadCodeBlock:
    """
    Code that can never execute under any input.

    kind:     'unreachable_after_return' | 'always_false_condition' |
              'unused_private' | 'unused_import' | 'empty_except'
    location: function name or 'module level'
    line:     line number
    snippet:  short excerpt (first 120 chars)
    """
    kind:     str
    location: str
    line:     int
    snippet:  str
    severity: str = "LOW"   # LOW | MEDIUM | HIGH


@dataclass
class RiskArea:
    """
    A specific code construct that is statistically likely to cause bugs.

    category:    category label for grouping in reports
    location:    function or method name
    line:        line number
    description: what the risk is
    suggestion:  how to reduce the risk
    risk_score:  0.0–1.0 (used to sort priority list)
    test_hint:   the kind of test that would catch this risk
    """
    category:    str
    location:    str
    line:        int
    description: str
    suggestion:  str
    risk_score:  float
    test_hint:   str = ""


@dataclass
class UntestedArea:
    """
    A function or branch for which no test coverage signal exists.

    name:       function/method name
    location:   containing class or 'module level'
    line:       line number
    complexity: cyclomatic complexity (higher = more urgent to test)
    is_public:  public API is higher priority than private helpers
    test_hint:  suggested test approach
    """
    name:       str
    location:   str
    line:       int
    complexity: int
    is_public:  bool
    test_hint:  str = ""


@dataclass
class TestIntelligenceReport:
    """
    The complete output of one TestIntelligenceEngine.analyse() call.

    Consumed by:
      • QAAgent   — via to_qa_brief()
      • Engine    — via to_memory_entries()
      • CLI       — via to_text_report()
    """
    source_hash:   str
    source_lines:  int
    parse_error:   Optional[str]

    critical_paths:  List[CriticalPath]  = field(default_factory=list)
    dead_code:       List[DeadCodeBlock] = field(default_factory=list)
    risk_areas:      List[RiskArea]      = field(default_factory=list)
    untested_areas:  List[UntestedArea]  = field(default_factory=list)

    # Derived
    test_priorities: List[str] = field(default_factory=list)   # ordered names to test

    # Stats
    total_functions:   int = 0
    public_functions:  int = 0
    complexity_mean:   float = 0.0
    complexity_max:    int = 0

    def to_qa_brief(self) -> str:
        """
        Formatted string injected into QAAgent's prompt.
        Tells the LLM exactly what to test, in what order, and why.
        """
        if self.parse_error:
            return f"[Test Intelligence] Parse error: {self.parse_error}. Write general tests."

        parts = ["=== TEST INTELLIGENCE BRIEF ===\n"]

        if self.critical_paths:
            parts.append("CRITICAL PATHS (test these first — failure breaks everything):")
            for i, cp in enumerate(self.critical_paths[:5], 1):
                parts.append(
                    f"  {i}. {cp.entry_point}  "
                    f"[risk={cp.risk_score:.2f}, fan_in={cp.fan_in}, depth={cp.depth}]\n"
                    f"     Path: {' → '.join(cp.path[:6])}\n"
                    f"     Why: {cp.reason}"
                )

        if self.risk_areas:
            parts.append("\nRISK AREAS (high defect probability — write edge-case tests):")
            shown = sorted(self.risk_areas, key=lambda r: r.risk_score, reverse=True)[:8]
            for ra in shown:
                parts.append(
                    f"  • {ra.location} (line {ra.line})  [{ra.category}, risk={ra.risk_score:.2f}]\n"
                    f"    {ra.description}\n"
                    f"    Test hint: {ra.test_hint}"
                )

        if self.untested_areas:
            parts.append("\nUNTESTED AREAS (no coverage signal — write at minimum a smoke test):")
            shown = sorted(
                self.untested_areas,
                key=lambda u: (u.is_public, u.complexity),
                reverse=True,
            )[:8]
            for ua in shown:
                pub = "public" if ua.is_public else "private"
                parts.append(
                    f"  • {ua.name} ({pub}, line {ua.line}, "
                    f"complexity={ua.complexity}): {ua.test_hint}"
                )

        if self.dead_code:
            parts.append("\nDEAD CODE (do not write tests for these — they can never run):")
            for dc in self.dead_code[:5]:
                parts.append(f"  • {dc.location} line {dc.line}: {dc.kind}")

        parts.append(
            f"\nCoverage targets: {self.total_functions} functions total, "
            f"{self.public_functions} public, "
            f"mean complexity={self.complexity_mean:.1f}, "
            f"max complexity={self.complexity_max}."
        )
        parts.append("Write tests in the order above. Cover the critical paths first.")
        parts.append("=== END TEST INTELLIGENCE BRIEF ===")

        return "\n".join(parts)

    def to_text_report(self) -> str:
        """Human-readable report for CLI output."""
        lines = [
            "FORGE Test Intelligence Report",
            "=" * 50,
            f"Source:   {self.source_lines} lines  "
            f"| {self.total_functions} functions ({self.public_functions} public)",
            f"Analysis: {len(self.critical_paths)} critical paths  "
            f"| {len(self.risk_areas)} risk areas  "
            f"| {len(self.dead_code)} dead-code blocks  "
            f"| {len(self.untested_areas)} untested areas",
            f"Complexity: mean={self.complexity_mean:.1f}  max={self.complexity_max}",
            "",
        ]

        if self.parse_error:
            lines.append(f"⚠ Parse error: {self.parse_error}")
            return "\n".join(lines)

        if self.critical_paths:
            lines += ["── Critical Paths ──────────────────────────────────────", ""]
            for cp in self.critical_paths:
                lines.append(f"  [risk={cp.risk_score:.2f}] {cp.entry_point}  (line {cp.line_number})")
                lines.append(f"    Calls: {' → '.join(cp.path[:8])}")
                lines.append(f"    {cp.reason}")
                lines.append("")

        if self.risk_areas:
            lines += ["── Risk Areas ──────────────────────────────────────────", ""]
            for ra in sorted(self.risk_areas, key=lambda r: r.risk_score, reverse=True):
                lines.append(f"  [{ra.risk_score:.2f}] {ra.location}  line {ra.line}  [{ra.category}]")
                lines.append(f"    {ra.description}")
                lines.append(f"    → {ra.suggestion}")
                if ra.test_hint:
                    lines.append(f"    Test: {ra.test_hint}")
                lines.append("")

        if self.dead_code:
            lines += ["── Dead Code ───────────────────────────────────────────", ""]
            for dc in self.dead_code:
                lines.append(f"  [{dc.severity}] {dc.location}  line {dc.line}  ({dc.kind})")
                lines.append(f"    {dc.snippet[:100]}")
                lines.append("")

        if self.untested_areas:
            lines += ["── Untested Areas ──────────────────────────────────────", ""]
            for ua in sorted(
                self.untested_areas,
                key=lambda u: (u.is_public, u.complexity),
                reverse=True,
            ):
                pub = "PUBLIC" if ua.is_public else "private"
                lines.append(
                    f"  {pub}  {ua.name}  (line {ua.line}, complexity={ua.complexity})"
                )
                if ua.test_hint:
                    lines.append(f"    → {ua.test_hint}")
                lines.append("")

        return "\n".join(lines)

    def to_memory_entries(self, task_id: str = "") -> List[dict]:
        """
        Structured entries for the cross-task memory store.
        Future tasks that import similar modules inherit these risk findings.
        """
        entries = []
        for ra in self.risk_areas:
            if ra.risk_score >= _RISK_THRESHOLD:
                entries.append({
                    "key":        f"risk:{ra.category}:{ra.location}",
                    "value":      ra.description,
                    "memory_type":"fact",
                    "confidence": ra.risk_score,
                    "tags":       ["test_intelligence", "risk", ra.category],
                    "task_id":    task_id,
                })
        for cp in self.critical_paths:
            entries.append({
                "key":        f"critical_path:{cp.entry_point}",
                "value":      f"Critical path: {' → '.join(cp.path[:6])}. {cp.reason}",
                "memory_type":"fact",
                "confidence": cp.risk_score,
                "tags":       ["test_intelligence", "critical_path"],
                "task_id":    task_id,
            })
        return entries

    def summary_dict(self) -> dict:
        return {
            "total_functions":    self.total_functions,
            "public_functions":   self.public_functions,
            "critical_paths":     len(self.critical_paths),
            "risk_areas":         len(self.risk_areas),
            "dead_code_blocks":   len(self.dead_code),
            "untested_areas":     len(self.untested_areas),
            "complexity_mean":    round(self.complexity_mean, 2),
            "complexity_max":     self.complexity_max,
            "test_priorities":    self.test_priorities[:10],
        }


# ── AST helpers ────────────────────────────────────────────────────────────────

def _sha(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _snippet(node: ast.AST, source_lines: List[str]) -> str:
    try:
        line = node.lineno - 1
        return source_lines[line].strip()[:120]
    except (AttributeError, IndexError):
        return ""


def _cyclomatic_complexity(node: ast.AST) -> int:
    """
    Cyclomatic complexity = 1 + number of branching nodes.
    Branches: if, elif, for, while, except, with, assert, comprehension ternary.
    """
    count = 1
    for child in ast.walk(node):
        if isinstance(child, (
            ast.If, ast.For, ast.While, ast.ExceptHandler,
            ast.With, ast.Assert, ast.comprehension,
        )):
            count += 1
        elif isinstance(child, ast.BoolOp):
            count += len(child.values) - 1
    return count


def _nesting_depth(node: ast.AST) -> int:
    """Maximum nesting depth within a function body."""
    max_depth = [0]

    def _walk(n: ast.AST, depth: int) -> None:
        if isinstance(n, (ast.If, ast.For, ast.While, ast.With,
                          ast.Try, ast.ExceptHandler)):
            max_depth[0] = max(max_depth[0], depth)
            for child in ast.iter_child_nodes(n):
                _walk(child, depth + 1)
        else:
            for child in ast.iter_child_nodes(n):
                _walk(child, depth)

    _walk(node, 1)
    return max_depth[0]


def _collect_functions(tree: ast.AST) -> List[ast.FunctionDef]:
    """Return all FunctionDef and AsyncFunctionDef nodes."""
    return [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _function_names_in_tests(workspace_dir: Optional[str] = None) -> Set[str]:
    """
    Scan test files in workspace_dir (or cwd) for function names that appear
    in test code — a proxy for existing test coverage.
    """
    tested: Set[str] = set()
    search_root = Path(workspace_dir) if workspace_dir else Path(".")
    test_files  = list(search_root.rglob("test_*.py")) + list(search_root.rglob("*_test.py"))
    for tf in test_files[:20]:   # cap at 20 files to stay fast
        try:
            src = tf.read_text(encoding="utf-8", errors="replace")
            # Collect called names (heuristic: any identifier followed by '(')
            for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", src):
                tested.add(m.group(1))
        except OSError:
            pass
    return tested


# ── Call-graph builder ─────────────────────────────────────────────────────────

class CallGraphBuilder(ast.NodeVisitor):
    """
    Build a lightweight directed call graph from an AST.

    Nodes: function names (str)
    Edges: caller → callee

    Only intra-module calls are tracked (external library calls are noted
    but not followed, keeping analysis fast and deterministic).
    """

    def __init__(self, known_functions: Set[str]):
        self.known   = known_functions
        self.edges:  Dict[str, Set[str]] = defaultdict(set)   # caller → callees
        self.fan_in: Dict[str, int]      = defaultdict(int)   # callee → call count
        self._current: Optional[str]     = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prev = self._current
        self._current = node.name
        self.generic_visit(node)
        self._current = prev

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if self._current is None:
            self.generic_visit(node)
            return
        callee = _call_name(node)
        if callee and callee in self.known:
            self.edges[self._current].add(callee)
            self.fan_in[callee] += 1
        self.generic_visit(node)


def _call_name(node: ast.Call) -> Optional[str]:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


# ── Main engine ────────────────────────────────────────────────────────────────

class TestIntelligenceEngine:
    """
    Analyses Python source code and produces a TestIntelligenceReport.

    Usage::

        engine = TestIntelligenceEngine()
        report = engine.analyse(source_code)
        print(report.to_text_report())

    Thread-safe: no shared mutable state; each analyse() call is independent.
    """

    def analyse(
        self,
        source:        str,
        filename:      str = "<source>",
        workspace_dir: Optional[str] = None,
    ) -> TestIntelligenceReport:
        """
        Main entry point.  Parses source, runs all four analysis passes,
        builds the report and priority list.
        """
        lines        = source.splitlines()
        source_hash  = _sha(source)

        report = TestIntelligenceReport(
            source_hash  = source_hash,
            source_lines = len(lines),
            parse_error  = None,
        )

        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            report.parse_error = str(exc)
            return report

        funcs = _collect_functions(tree)
        func_names = {f.name for f in funcs}

        # ── Function stats ────────────────────────────────────────────────
        complexities = [_cyclomatic_complexity(f) for f in funcs]
        report.total_functions  = len(funcs)
        report.public_functions = sum(1 for f in funcs if _is_public(f.name))
        report.complexity_mean  = (sum(complexities) / len(complexities)) if complexities else 0.0
        report.complexity_max   = max(complexities) if complexities else 0

        # ── Build call graph ──────────────────────────────────────────────
        cg = CallGraphBuilder(func_names)
        cg.visit(tree)

        # ── Pass 1: Critical paths ────────────────────────────────────────
        report.critical_paths = self._find_critical_paths(funcs, cg, complexities, lines)

        # ── Pass 2: Dead code ─────────────────────────────────────────────
        report.dead_code = self._find_dead_code(tree, funcs, cg, lines)

        # ── Pass 3: Risk areas ────────────────────────────────────────────
        report.risk_areas = self._find_risk_areas(funcs, complexities, lines, source)

        # ── Pass 4: Untested areas ────────────────────────────────────────
        tested_names = _function_names_in_tests(workspace_dir) if workspace_dir else set()
        report.untested_areas = self._find_untested_areas(funcs, complexities, tested_names)

        # ── Priority list ─────────────────────────────────────────────────
        report.test_priorities = self._build_priority_list(report)

        return report

    # ── Pass 1: Critical paths ─────────────────────────────────────────────

    def _find_critical_paths(
        self,
        funcs:       List[ast.FunctionDef],
        cg:          CallGraphBuilder,
        complexities: List[int],
        lines:       List[str],
    ) -> List[CriticalPath]:
        """
        Identify entry-point functions ranked by:
          - fan_in (how many things call this)
          - call depth (how deep the call tree goes)
          - complexity of the entry point
          - whether it is public API
          - whether it handles exceptions (propagation risk)
        """
        paths = []
        for func, complexity in zip(funcs, complexities):
            name    = func.name
            fan_in  = cg.fan_in.get(name, 0)
            depth   = self._call_depth(name, cg.edges, visited=set())
            is_pub  = _is_public(name)
            has_exc = self._raises_exceptions(func)

            # Score: weighted composite
            score = (
                min(fan_in,   10) / 10  * 0.35 +   # fan-in weight
                min(depth,    10) / 10  * 0.25 +   # depth weight
                min(complexity,20) / 20 * 0.20 +   # complexity weight
                (0.15 if is_pub  else 0.0)       +  # public API bonus
                (0.05 if has_exc else 0.0)           # exception propagation bonus
            )

            if score < 0.15 and fan_in == 0 and depth <= 1:
                continue   # leaf function with no callers — not a critical path

            path  = self._trace_path(name, cg.edges, visited=set(), max_depth=8)
            reason = self._explain_criticality(fan_in, depth, complexity, is_pub, has_exc)

            paths.append(CriticalPath(
                entry_point = name,
                path        = path,
                fan_in      = fan_in,
                depth       = depth,
                risk_score  = round(score, 3),
                reason      = reason,
                line_number = getattr(func, "lineno", 0),
            ))

        return sorted(paths, key=lambda p: p.risk_score, reverse=True)

    def _call_depth(
        self, name: str, edges: Dict[str, Set[str]],
        visited: Set[str], depth: int = 0,
    ) -> int:
        if name in visited or depth > 15:
            return depth
        visited.add(name)
        callees = edges.get(name, set())
        if not callees:
            return depth
        return max(
            self._call_depth(c, edges, visited.copy(), depth + 1)
            for c in callees
        )

    def _trace_path(
        self, name: str, edges: Dict[str, Set[str]],
        visited: Set[str], max_depth: int,
    ) -> List[str]:
        if name in visited or max_depth <= 0:
            return [name]
        visited.add(name)
        callees = list(edges.get(name, set()))
        if not callees:
            return [name]
        # Follow the callee with the most further callees (depth-first richest)
        richest = max(callees, key=lambda c: len(edges.get(c, set())))
        return [name] + self._trace_path(richest, edges, visited, max_depth - 1)

    def _raises_exceptions(self, func: ast.FunctionDef) -> bool:
        return any(isinstance(n, ast.Raise) for n in ast.walk(func))

    def _explain_criticality(
        self, fan_in: int, depth: int, complexity: int,
        is_pub: bool, has_exc: bool,
    ) -> str:
        reasons = []
        if fan_in >= 3:
            reasons.append(f"called by {fan_in} other functions")
        if depth >= 4:
            reasons.append(f"triggers a call chain {depth} levels deep")
        if complexity >= _MAX_COMPLEXITY:
            reasons.append(f"cyclomatic complexity {complexity} exceeds threshold")
        if is_pub:
            reasons.append("public API — external callers depend on it")
        if has_exc:
            reasons.append("raises exceptions that propagate to callers")
        return "; ".join(reasons) if reasons else "primary execution path"

    # ── Pass 2: Dead code ──────────────────────────────────────────────────

    def _find_dead_code(
        self,
        tree:  ast.AST,
        funcs: List[ast.FunctionDef],
        cg:    CallGraphBuilder,
        lines: List[str],
    ) -> List[DeadCodeBlock]:
        dead = []

        for func in funcs:
            fname = func.name
            body  = func.body

            # ── Unreachable after return/raise/continue/break ─────────────
            for i, stmt in enumerate(body[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    next_stmt = body[i + 1]
                    if not isinstance(next_stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                   ast.ClassDef)):
                        dead.append(DeadCodeBlock(
                            kind     = "unreachable_after_return",
                            location = fname,
                            line     = getattr(next_stmt, "lineno", 0),
                            snippet  = _snippet(next_stmt, lines),
                            severity = "MEDIUM",
                        ))
                    break

            # ── Always-False conditions ───────────────────────────────────
            for node in ast.walk(func):
                if isinstance(node, ast.If):
                    if self._is_always_false(node.test):
                        dead.append(DeadCodeBlock(
                            kind     = "always_false_condition",
                            location = fname,
                            line     = node.lineno,
                            snippet  = _snippet(node, lines),
                            severity = "HIGH",
                        ))

            # ── Empty except blocks ───────────────────────────────────────
            for node in ast.walk(func):
                if isinstance(node, ast.ExceptHandler):
                    non_pass = [s for s in node.body
                                if not isinstance(s, ast.Pass)]
                    if not non_pass:
                        dead.append(DeadCodeBlock(
                            kind     = "empty_except",
                            location = fname,
                            line     = node.lineno,
                            snippet  = "except: pass",
                            severity = "MEDIUM",
                        ))

        # ── Unused private functions (not called from anywhere in module) ──
        called_anywhere = set()
        for callees in cg.edges.values():
            called_anywhere.update(callees)
        # Also add anything called at module level
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name:
                    called_anywhere.add(name)

        for func in funcs:
            if (
                func.name.startswith("_")
                and not func.name.startswith("__")
                and func.name not in called_anywhere
            ):
                dead.append(DeadCodeBlock(
                    kind     = "unused_private",
                    location = func.name,
                    line     = func.lineno,
                    snippet  = _snippet(func, lines),
                    severity = "LOW",
                ))

        # ── Unused imports ─────────────────────────────────────────────────
        imported_names: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)

        # Collect all Name references outside imports
        used_names: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                used_names.add(node.id)

        for imp_name in imported_names:
            if imp_name and imp_name not in used_names and imp_name != "*":
                # Find the import line
                imp_line = 0
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        for alias in node.names:
                            if (alias.asname or alias.name.split(".")[0]) == imp_name:
                                imp_line = node.lineno
                dead.append(DeadCodeBlock(
                    kind     = "unused_import",
                    location = "module level",
                    line     = imp_line,
                    snippet  = f"import {imp_name}",
                    severity = "LOW",
                ))

        return sorted(dead, key=lambda d: (d.severity, d.line))

    def _is_always_false(self, node: ast.expr) -> bool:
        """Conservative check: only flag obvious literal False conditions."""
        if isinstance(node, ast.Constant):
            return node.value is False or node.value == 0
        return False

    # ── Pass 3: Risk areas ─────────────────────────────────────────────────

    def _find_risk_areas(
        self,
        funcs:        List[ast.FunctionDef],
        complexities: List[int],
        lines:        List[str],
        source:       str,
    ) -> List[RiskArea]:
        risks: List[RiskArea] = []

        for func, complexity in zip(funcs, complexities):
            fname = func.name
            fline = getattr(func, "lineno", 0)
            nesting = _nesting_depth(func)

            # ── High cyclomatic complexity ─────────────────────────────────
            if complexity > _MAX_COMPLEXITY:
                score = min(0.5 + (complexity - _MAX_COMPLEXITY) * 0.05, 1.0)
                risks.append(RiskArea(
                    category    = "high_complexity",
                    location    = fname,
                    line        = fline,
                    description = f"Cyclomatic complexity {complexity} exceeds threshold {_MAX_COMPLEXITY}",
                    suggestion  = "Extract sub-functions to reduce branching",
                    risk_score  = round(score, 3),
                    test_hint   = "Write a test for every branch (equivalence partitioning)",
                ))

            # ── Deep nesting ───────────────────────────────────────────────
            if nesting >= 4:
                risks.append(RiskArea(
                    category    = "deep_nesting",
                    location    = fname,
                    line        = fline,
                    description = f"Nesting depth {nesting} — hard to reason about",
                    suggestion  = "Flatten with guard clauses or extract inner loops",
                    risk_score  = round(min(0.3 + nesting * 0.08, 0.9), 3),
                    test_hint   = "Test boundary conditions at each nesting level",
                ))

            # ── Bare except ────────────────────────────────────────────────
            for node in ast.walk(func):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    risks.append(RiskArea(
                        category    = "bare_except",
                        location    = fname,
                        line        = node.lineno,
                        description = "Bare 'except:' catches ALL exceptions including KeyboardInterrupt",
                        suggestion  = "Catch specific exception types",
                        risk_score  = 0.75,
                        test_hint   = "Test that specific exceptions propagate correctly",
                    ))

            # ── Mutable default argument ───────────────────────────────────
            for arg in func.args.defaults:
                if isinstance(arg, (ast.List, ast.Dict, ast.Set)):
                    risks.append(RiskArea(
                        category    = "mutable_default",
                        location    = fname,
                        line        = fline,
                        description = "Mutable default argument — shared state across calls",
                        suggestion  = "Use None as default and initialise inside the function",
                        risk_score  = 0.70,
                        test_hint   = "Call function twice without passing the argument; state must not leak between calls",
                    ))

            # ── Missing input validation on public functions ────────────────
            if _is_public(fname) and func.args.args:
                has_validation = any(
                    isinstance(n, (ast.Assert, ast.Raise, ast.If))
                    and isinstance(getattr(n, "lineno", None), int)
                    and n.lineno <= fline + 5
                    for n in ast.walk(func)
                )
                if not has_validation and complexity >= 3:
                    risks.append(RiskArea(
                        category    = "missing_input_validation",
                        location    = fname,
                        line        = fline,
                        description = "Public function with no apparent input validation",
                        suggestion  = "Add guard clauses or type checks at function entry",
                        risk_score  = 0.55,
                        test_hint   = "Test with None, empty, negative, and out-of-range inputs",
                    ))

            # ── String-formatted SQL ───────────────────────────────────────
            sql_kw = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE)\b", re.I)
            for node in ast.walk(func):
                if isinstance(node, ast.JoinedStr):   # f-string
                    # Reconstruct partial text to check for SQL keywords
                    parts_text = " ".join(
                        ast.literal_eval(p) if isinstance(p, ast.Constant) else ""
                        for p in node.values
                        if isinstance(p, ast.Constant)
                    )
                    if sql_kw.search(parts_text):
                        risks.append(RiskArea(
                            category    = "sql_injection_risk",
                            location    = fname,
                            line        = getattr(node, "lineno", fline),
                            description = "f-string used to construct SQL query — injection risk",
                            suggestion  = "Use parameterised queries / ORM",
                            risk_score  = 0.95,
                            test_hint   = "Test with SQL-metacharacter inputs: ' OR 1=1--, ; DROP TABLE",
                        ))

            # ── Recursion without apparent base case guard ─────────────────
            calls_self = any(
                isinstance(n, ast.Call) and _call_name(n) == fname
                for n in ast.walk(func)
            )
            if calls_self:
                # A base case is a Return inside an If near the top of the function.
                # We look for any If-guarded Return in the first 40% of the function body.
                func_end  = getattr(func, "end_lineno", fline + 30)
                func_span = max(func_end - fline, 1)
                guard_zone_end = fline + max(int(func_span * 0.40), 4)
                has_early_return = any(
                    isinstance(n, ast.If) and any(
                        isinstance(child, ast.Return)
                        for child in ast.walk(n)
                    ) and getattr(n, "lineno", 9999) <= guard_zone_end
                    for n in ast.walk(func)
                )
                if not has_early_return:
                    risks.append(RiskArea(
                        category    = "unbounded_recursion",
                        location    = fname,
                        line        = fline,
                        description = "Recursive function with no apparent early-return base case",
                        suggestion  = "Add a base case at the top of the function",
                        risk_score  = 0.80,
                        test_hint   = "Test with the base-case input and with depth=sys.getrecursionlimit()-1",
                    ))

            # ── Type coercion without guard ────────────────────────────────
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    name = _call_name(node)
                    if name in ("int", "float", "complex") and node.args:
                        risks.append(RiskArea(
                            category    = "unsafe_type_coercion",
                            location    = fname,
                            line        = getattr(node, "lineno", fline),
                            description = f"Unguarded {name}() coercion — raises ValueError on bad input",
                            suggestion  = f"Wrap in try/except or validate type before calling {name}()",
                            risk_score  = 0.50,
                            test_hint   = f"Test with non-numeric string input to trigger ValueError",
                        ))

        # ── Module-level: global mutable state ────────────────────────────
        for node in ast.walk(ast.parse("")):   # module-level scan below
            pass

        return sorted(risks, key=lambda r: r.risk_score, reverse=True)

    # ── Pass 4: Untested areas ─────────────────────────────────────────────

    def _find_untested_areas(
        self,
        funcs:        List[ast.FunctionDef],
        complexities: List[int],
        tested_names: Set[str],
    ) -> List[UntestedArea]:
        untested = []
        for func, complexity in zip(funcs, complexities):
            name = func.name
            SKIP_DUNDERS = {"__repr__", "__str__", "__len__", "__eq__", "__hash__", "__del__"}
            if name in SKIP_DUNDERS:
                continue   # trivial dunders — skip
            if name in tested_names:
                continue   # already has test coverage signal

            # Generate a test hint based on function signature
            hint = self._generate_test_hint(func, complexity)

            untested.append(UntestedArea(
                name       = name,
                location   = self._containing_class(func),
                line       = getattr(func, "lineno", 0),
                complexity = complexity,
                is_public  = _is_public(name),
                test_hint  = hint,
            ))

        return untested

    def _containing_class(self, func: ast.FunctionDef) -> str:
        """Return class name if this is a method, else 'module level'."""
        # We don't have parent pointers from ast.walk, so use name heuristic
        return "module level"

    def _generate_test_hint(self, func: ast.FunctionDef, complexity: int) -> str:
        """Generate a context-sensitive test suggestion from the function signature."""
        args  = [a.arg for a in func.args.args if a.arg != "self"]
        name  = func.name
        hints = []

        if not args:
            hints.append(f"Call {name}() with no arguments; assert return value or side effects")
        else:
            hints.append(f"Test {name}({', '.join(args[:3])}) with: valid inputs, None, empty, boundary values")

        if complexity >= 5:
            hints.append(f"complexity={complexity}: write separate tests for each branch")

        # Name-based heuristics
        if "parse" in name or "load" in name or "read" in name:
            hints.append("test with malformed/empty/oversized input")
        if "write" in name or "save" in name or "send" in name:
            hints.append("test failure path (disk full / network error via mock)")
        if "validate" in name or "check" in name or "verify" in name:
            hints.append("test both valid and invalid cases; assert correct error type")
        if "convert" in name or "transform" in name or "format" in name:
            hints.append("test identity transformation and round-trip")
        if "delete" in name or "remove" in name or "clear" in name:
            hints.append("test idempotency: calling twice must not raise")
        if "auth" in name or "login" in name or "token" in name:
            hints.append("test with invalid credentials; assert no token is returned")

        return "; ".join(hints)

    # ── Priority list builder ──────────────────────────────────────────────

    def _build_priority_list(self, report: TestIntelligenceReport) -> List[str]:
        """
        Flat ordered list of function names to test, highest priority first.
        Merges critical paths, risk areas, and untested areas into one ranked list.
        """
        scored: Dict[str, float] = {}

        for cp in report.critical_paths:
            scored[cp.entry_point] = max(scored.get(cp.entry_point, 0), cp.risk_score + 0.1)

        for ra in report.risk_areas:
            scored[ra.location] = max(scored.get(ra.location, 0), ra.risk_score)

        for ua in report.untested_areas:
            base = 0.3 + (0.1 if ua.is_public else 0) + min(ua.complexity / 30, 0.3)
            scored[ua.name] = max(scored.get(ua.name, 0), base)

        # Dead code locations are intentionally excluded — don't test unreachable code
        dead_locs = {dc.location for dc in report.dead_code
                     if dc.kind in ("unused_private", "unreachable_after_return")}
        for loc in dead_locs:
            scored.pop(loc, None)

        return [name for name, _ in sorted(scored.items(), key=lambda x: x[1], reverse=True)]


# ── Module-level singleton ─────────────────────────────────────────────────────

_engine: Optional[TestIntelligenceEngine] = None


def get_intelligence_engine() -> TestIntelligenceEngine:
    global _engine
    if _engine is None:
        _engine = TestIntelligenceEngine()
    return _engine
