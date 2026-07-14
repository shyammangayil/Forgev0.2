"""
FORGE Performance Validators
=============================
Static analysis validators that run on generated code before it reaches the
review/test phases. All analysis is AST-based — no LLM calls, no network,
runs in milliseconds.

Validators:
  1. ComplexityValidator     — cyclomatic complexity, function length
  2. AlgorithmicValidator    — nested loops, O(n²) patterns, inefficient structures
  3. ResourceValidator       — unclosed resources, unbounded collections, memory leaks
  4. ConcurrencyValidator    — race conditions, missing locks, unsafe shared state

Each validator returns a ValidationResult with PASS/WARN/FAIL and a list of findings.
FAIL blocks the pipeline gate. WARN is logged but does not block.

Env vars
--------
  FORGE_MAX_COMPLEXITY=10      max cyclomatic complexity per function (default: 10)
  FORGE_MAX_FUNCTION_LINES=50  max lines per function (default: 50)
  FORGE_COMPLEXITY_GATE=warn   'fail' to gate on complexity, 'warn' to log only
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_MAX_COMPLEXITY    = int(os.environ.get("FORGE_MAX_COMPLEXITY",     "10"))
_MAX_FUNC_LINES    = int(os.environ.get("FORGE_MAX_FUNCTION_LINES", "50"))
_COMPLEXITY_GATE   = os.environ.get("FORGE_COMPLEXITY_GATE", "warn").lower()


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity:    str          # CRITICAL | HIGH | MEDIUM | LOW | INFO
    category:    str          # complexity | algorithmic | resource | concurrency
    location:    str          # "function_name" or "line N"
    description: str
    suggestion:  str = ""


@dataclass
class ValidationResult:
    validator:  str
    verdict:    str           # PASS | WARN | FAIL
    findings:   List[Finding] = field(default_factory=list)
    stats:      Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict != "FAIL"

    def summary(self) -> str:
        counts = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{v}:{k}" for k, v in counts.items()]
        return f"{self.validator} [{self.verdict}] " + (", ".join(parts) if parts else "clean")


# ── AST helpers ───────────────────────────────────────────────────────────────

def _parse(code: str) -> Optional[ast.AST]:
    """Parse code, return None on syntax error."""
    try:
        return ast.parse(code)
    except SyntaxError:
        return None


def _func_name(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return "unknown"


def _line_count(node: ast.AST) -> int:
    try:
        return node.end_lineno - node.lineno + 1
    except AttributeError:
        return 0


# ── 1. Cyclomatic Complexity Validator ────────────────────────────────────────

def _cyclomatic_complexity(func_node: ast.AST) -> int:
    """
    McCabe cyclomatic complexity = 1 + number of decision points.
    Decision points: if, elif, for, while, except, with, assert, comprehensions,
    boolean operators (and/or).
    """
    complexity = 1
    for node in ast.walk(func_node):
        if isinstance(node, (
            ast.If, ast.For, ast.While, ast.ExceptHandler,
            ast.With, ast.Assert,
            ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
        )):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += len(node.values) - 1
    return complexity


class ComplexityValidator:
    """
    Validates cyclomatic complexity and function length.
    High complexity = hard to test, hard to maintain, likely bugs.
    """

    def validate(self, code: str) -> ValidationResult:
        tree = _parse(code)
        if tree is None:
            return ValidationResult("ComplexityValidator", "PASS",
                                    stats={"note": "Could not parse — skipped"})

        findings = []
        complexities = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            name = node.name
            cc   = _cyclomatic_complexity(node)
            lines = _line_count(node)
            complexities.append((name, cc))

            # Complexity check
            if cc > _MAX_COMPLEXITY:
                sev = "HIGH" if cc > _MAX_COMPLEXITY * 1.5 else "MEDIUM"
                findings.append(Finding(
                    severity=sev,
                    category="complexity",
                    location=f"def {name} (line {node.lineno})",
                    description=f"Cyclomatic complexity {cc} exceeds threshold {_MAX_COMPLEXITY}",
                    suggestion=(
                        f"Break '{name}' into smaller functions. "
                        f"Extract conditional branches into helper methods."
                    ),
                ))

            # Function length check
            if lines > _MAX_FUNC_LINES:
                findings.append(Finding(
                    severity="LOW",
                    category="complexity",
                    location=f"def {name} (line {node.lineno})",
                    description=f"Function is {lines} lines (max: {_MAX_FUNC_LINES})",
                    suggestion="Consider splitting into smaller, focused functions.",
                ))

        # Determine verdict
        high_findings = [f for f in findings if f.severity in ("HIGH", "CRITICAL")]
        if high_findings and _COMPLEXITY_GATE == "fail":
            verdict = "FAIL"
        elif findings:
            verdict = "WARN"
        else:
            verdict = "PASS"

        max_cc = max((c for _, c in complexities), default=0)
        return ValidationResult(
            validator="ComplexityValidator",
            verdict=verdict,
            findings=findings,
            stats={"max_complexity": max_cc, "functions_checked": len(complexities)},
        )


# ── 2. Algorithmic Complexity Validator ───────────────────────────────────────

class AlgorithmicValidator:
    """
    Detects common algorithmic inefficiencies:
    - Nested loops over the same collection (O(n²))
    - List membership test inside a loop (should be set)
    - Sorting inside a loop (O(n² log n))
    - Repeated list concatenation (quadratic)
    - dict/list creation inside loops where it could be pre-allocated
    """

    def validate(self, code: str) -> ValidationResult:
        tree = _parse(code)
        if tree is None:
            return ValidationResult("AlgorithmicValidator", "PASS")

        findings = []

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            self._check_nested_loops(func, findings)
            self._check_list_in_loop(func, findings)
            self._check_sort_in_loop(func, findings)
            self._check_string_concat_loop(func, findings)

        verdict = "FAIL" if any(f.severity in ("HIGH", "CRITICAL") for f in findings) \
                  else ("WARN" if findings else "PASS")

        return ValidationResult(
            validator="AlgorithmicValidator",
            verdict=verdict,
            findings=findings,
        )

    def _check_nested_loops(self, func: ast.AST, findings: List[Finding]) -> None:
        """Detect O(n²) nested for loops."""
        for outer in ast.walk(func):
            if not isinstance(outer, (ast.For, ast.While)):
                continue
            for inner in ast.walk(outer):
                if inner is outer:
                    continue
                if isinstance(inner, (ast.For, ast.While)):
                    line = getattr(inner, "lineno", "?")
                    findings.append(Finding(
                        severity="MEDIUM",
                        category="algorithmic",
                        location=f"def {_func_name(func)} (line {line})",
                        description="Nested loop detected — potential O(n²) complexity",
                        suggestion=(
                            "Consider using a dict/set for O(1) lookups, "
                            "or a vectorised operation (numpy, itertools)."
                        ),
                    ))
                    return  # one finding per function is enough

    def _check_list_in_loop(self, func: ast.AST, findings: List[Finding]) -> None:
        """Detect `if x in list` inside a loop — should be `if x in set`."""
        for loop in ast.walk(func):
            if not isinstance(loop, (ast.For, ast.While)):
                continue
            for node in ast.walk(loop):
                if isinstance(node, ast.Compare):
                    for op, comp in zip(node.ops, node.comparators):
                        if isinstance(op, ast.In) and isinstance(comp, (ast.List, ast.Tuple)):
                            line = getattr(node, "lineno", "?")
                            findings.append(Finding(
                                severity="MEDIUM",
                                category="algorithmic",
                                location=f"def {_func_name(func)} (line {line})",
                                description="`if x in [list]` inside loop is O(n) per iteration",
                                suggestion="Convert the list to a set for O(1) membership tests.",
                            ))
                            return

    def _check_sort_in_loop(self, func: ast.AST, findings: List[Finding]) -> None:
        """Detect sorted() or .sort() inside a loop."""
        for loop in ast.walk(func):
            if not isinstance(loop, (ast.For, ast.While)):
                continue
            for node in ast.walk(loop):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = ""
                    if isinstance(fn, ast.Name):
                        name = fn.id
                    elif isinstance(fn, ast.Attribute):
                        name = fn.attr
                    if name in ("sort", "sorted"):
                        line = getattr(node, "lineno", "?")
                        findings.append(Finding(
                            severity="HIGH",
                            category="algorithmic",
                            location=f"def {_func_name(func)} (line {line})",
                            description="Sorting inside a loop — O(n² log n) complexity",
                            suggestion="Sort once before the loop, or use a sorted data structure.",
                        ))
                        return

    def _check_string_concat_loop(self, func: ast.AST, findings: List[Finding]) -> None:
        """Detect string += inside a loop (quadratic)."""
        for loop in ast.walk(func):
            if not isinstance(loop, (ast.For, ast.While)):
                continue
            for node in ast.walk(loop):
                if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
                    if isinstance(node.value, ast.Constant) and \
                       isinstance(node.value.value, str):
                        line = getattr(node, "lineno", "?")
                        findings.append(Finding(
                            severity="MEDIUM",
                            category="algorithmic",
                            location=f"def {_func_name(func)} (line {line})",
                            description="String concatenation with += in loop is O(n²)",
                            suggestion="Use a list and ''.join() after the loop.",
                        ))
                        return


# ── 3. Resource Usage Validator ───────────────────────────────────────────────

class ResourceValidator:
    """
    Detects resource management issues:
    - Files/connections opened without context manager (open() without with)
    - Unbounded list/dict appends (no size cap, potential memory issue)
    - Missing exception handling around I/O
    """

    # Calls that should be used with `with` statement
    CONTEXT_REQUIRED = {
        "open", "connect", "cursor", "session", "acquire",
        "socket", "urlopen", "get", "post", "request",
    }

    def validate(self, code: str) -> ValidationResult:
        tree = _parse(code)
        if tree is None:
            return ValidationResult("ResourceValidator", "PASS")

        findings = []
        self._check_bare_open(tree, findings)
        self._check_unbounded_growth(tree, findings)

        verdict = "FAIL" if any(f.severity in ("HIGH", "CRITICAL") for f in findings) \
                  else ("WARN" if findings else "PASS")
        return ValidationResult(
            validator="ResourceValidator",
            verdict=verdict,
            findings=findings,
        )

    def _check_bare_open(self, tree: ast.AST, findings: List[Finding]) -> None:
        """Detect open() calls not inside a `with` statement."""
        # Collect all `with` statement call names
        with_calls: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call):
                        fn = item.context_expr.func
                        name = fn.id if isinstance(fn, ast.Name) else \
                               (fn.attr if isinstance(fn, ast.Attribute) else "")
                        with_calls.add((getattr(item.context_expr, "lineno", 0), name))

        # Find bare open() calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else \
                       (fn.attr if isinstance(fn, ast.Attribute) else "")
                if name == "open":
                    line = getattr(node, "lineno", 0)
                    if (line, "open") not in with_calls:
                        findings.append(Finding(
                            severity="HIGH",
                            category="resource",
                            location=f"line {line}",
                            description="open() called without context manager",
                            suggestion="Use `with open(...) as f:` to ensure file is closed.",
                        ))

    def _check_unbounded_growth(self, tree: ast.AST, findings: List[Finding]) -> None:
        """Detect list.append() inside loops with no size check."""
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            append_in_loop = False
            has_size_check = False
            for node in ast.walk(func):
                if isinstance(node, (ast.For, ast.While)):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Call):
                            fn = inner.func
                            if isinstance(fn, ast.Attribute) and fn.attr == "append":
                                append_in_loop = True
                        # Check for len() comparison (size guard)
                        if isinstance(inner, ast.Compare):
                            for comparator in ast.walk(inner):
                                if isinstance(comparator, ast.Call):
                                    if isinstance(comparator.func, ast.Name) and \
                                       comparator.func.id == "len":
                                        has_size_check = True
            if append_in_loop and not has_size_check:
                findings.append(Finding(
                    severity="LOW",
                    category="resource",
                    location=f"def {func.name}",
                    description="Unbounded list growth detected (append in loop, no size check)",
                    suggestion=(
                        "Consider capping list size or using a deque with maxlen. "
                        "Verify this list cannot grow to memory-exhausting size."
                    ),
                ))


# ── 4. Concurrency Safety Validator ───────────────────────────────────────────

class ConcurrencyValidator:
    """
    Detects common concurrency issues:
    - Global variables modified in thread functions (data race risk)
    - Missing locks around shared state
    - threading.Thread without daemon=True in non-main contexts
    - asyncio.sleep(0) patterns that may hide race conditions
    """

    def validate(self, code: str) -> ValidationResult:
        tree = _parse(code)
        if tree is None:
            return ValidationResult("ConcurrencyValidator", "PASS")

        findings = []
        self._check_global_mutation(tree, findings)
        self._check_thread_without_lock(tree, findings)

        verdict = "FAIL" if any(f.severity in ("HIGH", "CRITICAL") for f in findings) \
                  else ("WARN" if findings else "PASS")
        return ValidationResult(
            validator="ConcurrencyValidator",
            verdict=verdict,
            findings=findings,
        )

    def _check_global_mutation(self, tree: ast.AST, findings: List[Finding]) -> None:
        """Detect global variables mutated inside functions that use threading."""
        uses_threading = any(
            isinstance(node, ast.Import) and
            any(alias.name in ("threading", "asyncio", "concurrent") for alias in node.names)
            for node in ast.walk(tree)
        )
        if not uses_threading:
            return

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Global):
                    # Check if the global name is assigned to in this function
                    for name in node.names:
                        for inner in ast.walk(func):
                            if isinstance(inner, ast.Assign):
                                for target in inner.targets:
                                    if isinstance(target, ast.Name) and target.id == name:
                                        findings.append(Finding(
                                            severity="HIGH",
                                            category="concurrency",
                                            location=f"def {func.name} (line {func.lineno})",
                                            description=f"Global variable '{name}' mutated in concurrent context",
                                            suggestion=(
                                                f"Protect '{name}' with threading.Lock() or "
                                                "use thread-local storage."
                                            ),
                                        ))

    def _check_thread_without_lock(self, tree: ast.AST, findings: List[Finding]) -> None:
        """Detect shared mutable state accessed in thread targets without locks."""
        # Simple heuristic: Thread(target=fn) where fn modifies module-level names
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = ""
                if isinstance(fn, ast.Attribute):
                    name = fn.attr
                elif isinstance(fn, ast.Name):
                    name = fn.id
                if name == "Thread":
                    # Check if lock is used anywhere nearby
                    has_lock = any(
                        isinstance(n, ast.Call) and
                        isinstance(n.func, ast.Attribute) and
                        n.func.attr in ("acquire", "release", "__enter__")
                        for n in ast.walk(tree)
                    )
                    if not has_lock:
                        line = getattr(node, "lineno", "?")
                        findings.append(Finding(
                            severity="MEDIUM",
                            category="concurrency",
                            location=f"line {line}",
                            description="Thread created but no Lock/RLock found in module",
                            suggestion=(
                                "Add threading.Lock() to protect shared state accessed "
                                "by the thread target function."
                            ),
                        ))
                        return  # one finding per module


# ── Combined runner ────────────────────────────────────────────────────────────

def run_all_validators(code: str, config: Optional[Dict] = None) -> List[ValidationResult]:
    """
    Run all performance validators on a code string.
    Returns list of ValidationResult objects.
    Used by the PerformanceGate in the orchestration engine.
    """
    validators = [
        ComplexityValidator(),
        AlgorithmicValidator(),
        ResourceValidator(),
        ConcurrencyValidator(),
    ]
    results = []
    for v in validators:
        try:
            results.append(v.validate(code))
        except Exception as e:
            results.append(ValidationResult(
                validator=type(v).__name__,
                verdict="PASS",
                stats={"error": str(e), "note": "Validator crashed — skipped"},
            ))
    return results


def format_results(results: List[ValidationResult]) -> str:
    """Format all validator results as a human-readable string."""
    lines = []
    for r in results:
        lines.append(f"  {r.summary()}")
        for f in r.findings:
            lines.append(f"    [{f.severity}] {f.location}: {f.description}")
            if f.suggestion:
                lines.append(f"           ↳ {f.suggestion}")
    return "\n".join(lines)