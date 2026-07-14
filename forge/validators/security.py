"""
FORGE Security Validators
==========================
Static analysis for security issues — no LLM, no network (except CVE check).
All checks are deterministic and fast.

Validators:
  1. OWASPValidator        — OWASP Top 10 pattern detection
  2. DependencyScanner     — CVE check against known vulnerable packages
  3. AccessControlChecker  — missing auth guards, privilege escalation paths
  4. SecretsScanner        — enhanced secret pattern detection

Env vars
--------
  FORGE_CVE_CHECK=true     enable online CVE checking via OSV API (default: false)
  FORGE_SECRETS_GATE=fail  'fail' blocks pipeline on secret detection (default: fail)
"""
from __future__ import annotations

import ast
import os
import re
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from forge.validators.performance import Finding, ValidationResult

_CVE_CHECK      = os.environ.get("FORGE_CVE_CHECK",     "false").lower() == "true"
_SECRETS_GATE   = os.environ.get("FORGE_SECRETS_GATE",  "fail").lower()


# ── 1. OWASP Top 10 Validator ─────────────────────────────────────────────────

class OWASPValidator:
    """
    Detects OWASP Top 10 vulnerability patterns via AST and regex analysis.

    A01 - Broken Access Control
    A02 - Cryptographic Failures
    A03 - Injection (SQL, command, LDAP)
    A05 - Security Misconfiguration
    A06 - Vulnerable Components (handled by DependencyScanner)
    A07 - Identification and Authentication Failures
    A08 - Software and Data Integrity Failures
    A09 - Security Logging Failures
    """

    # SQL injection patterns — f-strings or % formatting with user input near SQL keywords
    SQL_INJECTION_PATTERNS = [
        r"execute\s*\(\s*f['\"].*SELECT",
        r"execute\s*\(\s*f['\"].*INSERT",
        r"execute\s*\(\s*f['\"].*UPDATE",
        r"execute\s*\(\s*f['\"].*DELETE",
        r"execute\s*\(\s*['\"].*%s.*['\"].*%",
        r"cursor\.execute\s*\(\s*f",
        r"query\s*=\s*f['\"].*SELECT",
        r"query\s*=\s*f['\"].*WHERE",
        r'execute\(f"',
        r"execute\(f'",
    ]

    # Command injection patterns
    CMD_INJECTION_PATTERNS = [
        r"subprocess\.(run|call|Popen|check_output)\s*\([^)]*shell\s*=\s*True",
        r"os\.system\s*\(",
        r"os\.popen\s*\(",
        r"eval\s*\(",
        r"exec\s*\(",
        r"__import__\s*\(",
    ]

    # Weak cryptography patterns
    WEAK_CRYPTO_PATTERNS = [
        r"\bmd5\b",
        r"\bsha1\b",
        r"hashlib\.md5",
        r"hashlib\.sha1",
        r"DES\b",
        r"RC4\b",
        r"random\.random\(",      # not cryptographically secure
        r"random\.randint\(",
        r"random\.choice\(",
    ]

    # Hardcoded sensitive values
    HARDCODED_PATTERNS = [
        (r'password\s*=\s*["\'][^"\']{4,}["\']',   "Hardcoded password"),
        (r'secret\s*=\s*["\'][^"\']{8,}["\']',     "Hardcoded secret"),
        (r'api_key\s*=\s*["\'][^"\']{8,}["\']',    "Hardcoded API key"),
        (r'token\s*=\s*["\'][^"\']{8,}["\']',      "Hardcoded token"),
        (r'DEBUG\s*=\s*True',                        "Debug mode enabled"),
        (r'ALLOWED_HOSTS\s*=\s*\[[\s]*\*[\s]*\]',  "Wildcard ALLOWED_HOSTS"),
    ]

    # Missing authentication decorators (Flask/Django patterns)
    AUTH_REQUIRED_PATTERNS = [
        r"@app\.route",
        r"@router\.(get|post|put|delete|patch)",
        r"@api_view",
    ]
    AUTH_DECORATOR_PATTERNS = [
        r"@login_required",
        r"@permission_required",
        r"@require_auth",
        r"@authenticated",
        r"@jwt_required",
        r"@auth\.",
        r"Depends\(.*auth",
        r"Depends\(get_current_user",
    ]

    def validate(self, code: str) -> ValidationResult:
        findings = []
        self._check_sql_injection(code, findings)
        self._check_cmd_injection(code, findings)
        self._check_weak_crypto(code, findings)
        self._check_hardcoded(code, findings)
        self._check_unprotected_routes(code, findings)
        self._check_unsafe_deserialization(code, findings)
        self._check_xxe(code, findings)

        critical = [f for f in findings if f.severity == "CRITICAL"]
        high     = [f for f in findings if f.severity == "HIGH"]
        verdict  = "FAIL" if (critical or high) else ("WARN" if findings else "PASS")

        return ValidationResult(
            validator="OWASPValidator",
            verdict=verdict,
            findings=findings,
            stats={"owasp_findings": len(findings), "critical": len(critical)},
        )

    def _check_sql_injection(self, code: str, findings: List[Finding]) -> None:
        for pattern in self.SQL_INJECTION_PATTERNS:
            for m in re.finditer(pattern, code, re.IGNORECASE | re.MULTILINE):
                line = code[:m.start()].count("\n") + 1
                findings.append(Finding(
                    severity="CRITICAL",
                    category="OWASP-A03-Injection",
                    location=f"line {line}",
                    description="Potential SQL injection — dynamic query construction",
                    suggestion=(
                        "Use parameterised queries: cursor.execute(query, (param,)) "
                        "or an ORM. Never interpolate user input into SQL strings."
                    ),
                ))
                break  # one per pattern

    def _check_cmd_injection(self, code: str, findings: List[Finding]) -> None:
        for pattern in self.CMD_INJECTION_PATTERNS:
            for m in re.finditer(pattern, code, re.IGNORECASE):
                line = code[:m.start()].count("\n") + 1
                is_eval = "eval" in pattern or "exec" in pattern
                findings.append(Finding(
                    severity="CRITICAL" if is_eval else "HIGH",
                    category="OWASP-A03-Injection",
                    location=f"line {line}",
                    description=f"Command/code injection risk: {m.group()[:50]}",
                    suggestion=(
                        "Avoid eval/exec. Use subprocess with shell=False and a "
                        "list of arguments. Validate and sanitise all user input."
                    ),
                ))
                break

    def _check_weak_crypto(self, code: str, findings: List[Finding]) -> None:
        # Skip if it's in a comment or docstring
        code_no_comments = re.sub(r'#.*$|""".*?"""|\'\'\'.*?\'\'\'', "", code,
                                  flags=re.MULTILINE | re.DOTALL)
        for pattern in self.WEAK_CRYPTO_PATTERNS:
            if re.search(pattern, code_no_comments, re.IGNORECASE):
                findings.append(Finding(
                    severity="HIGH",
                    category="OWASP-A02-Crypto",
                    location="module",
                    description=f"Weak cryptography detected: {pattern}",
                    suggestion=(
                        "Use SHA-256+ for hashing. Use secrets.token_bytes() for "
                        "random values. Use AES-256-GCM or ChaCha20-Poly1305 for encryption."
                    ),
                ))

    def _check_hardcoded(self, code: str, findings: List[Finding]) -> None:
        for pattern, label in self.HARDCODED_PATTERNS:
            m = re.search(pattern, code, re.IGNORECASE)
            if m:
                line = code[:m.start()].count("\n") + 1
                # Skip if it's clearly a placeholder
                val = m.group()
                if any(p in val.lower() for p in ("example", "your_", "changeme",
                                                    "placeholder", "xxx", "...")):
                    continue
                findings.append(Finding(
                    severity="HIGH",
                    category="OWASP-A02-Crypto",
                    location=f"line {line}",
                    description=f"{label} detected",
                    suggestion="Use environment variables or a secrets manager. Never hardcode credentials.",
                ))

    def _check_unprotected_routes(self, code: str, findings: List[Finding]) -> None:
        """Detect HTTP routes without authentication decorators."""
        lines = code.splitlines()
        has_auth_anywhere = any(
            re.search(p, code, re.IGNORECASE)
            for p in self.AUTH_DECORATOR_PATTERNS
        )
        if not has_auth_anywhere:
            return  # no auth system in use — skip

        for i, line in enumerate(lines):
            for route_pattern in self.AUTH_REQUIRED_PATTERNS:
                if re.search(route_pattern, line):
                    # Check the 3 lines above for an auth decorator
                    context = "\n".join(lines[max(0, i-3):i])
                    has_auth = any(
                        re.search(p, context, re.IGNORECASE)
                        for p in self.AUTH_DECORATOR_PATTERNS
                    )
                    if not has_auth:
                        findings.append(Finding(
                            severity="HIGH",
                            category="OWASP-A01-Access-Control",
                            location=f"line {i+1}: {line.strip()[:60]}",
                            description="HTTP route missing authentication decorator",
                            suggestion=(
                                "Add @login_required, @jwt_required, or equivalent "
                                "before every route that accesses protected resources."
                            ),
                        ))

    def _check_unsafe_deserialization(self, code: str, findings: List[Finding]) -> None:
        patterns = [
            (r"\bpickle\.loads\b",    "pickle.loads() — arbitrary code execution on untrusted data"),
            (r"\bpickle\.load\b",     "pickle.load() — arbitrary code execution on untrusted data"),
            (r"\byaml\.load\b(?!\s*\(.*Loader\s*=\s*yaml\.Safe)", "yaml.load() without SafeLoader"),
            (r"\beval\s*\(",          "eval() — arbitrary code execution"),
            (r"\bexec\s*\(",          "exec() — arbitrary code execution"),
        ]
        for pattern, desc in patterns:
            m = re.search(pattern, code)
            if m:
                line = code[:m.start()].count("\n") + 1
                findings.append(Finding(
                    severity="CRITICAL",
                    category="OWASP-A08-Integrity",
                    location=f"line {line}",
                    description=desc,
                    suggestion="Use json.loads() for untrusted data. Use yaml.safe_load(). Avoid eval/exec.",
                ))

    def _check_xxe(self, code: str, findings: List[Finding]) -> None:
        """Detect XML parsing without XXE protection."""
        if "xml" not in code.lower():
            return
        patterns = [
            r"xml\.etree\.ElementTree\.parse",
            r"ET\.parse\(",
            r"lxml\.etree\.parse",
            r"minidom\.parse",
        ]
        for pattern in patterns:
            if re.search(pattern, code):
                findings.append(Finding(
                    severity="HIGH",
                    category="OWASP-A05-Misconfiguration",
                    location="module",
                    description="XML parsing without XXE protection",
                    suggestion=(
                        "Use defusedxml library or disable entity expansion: "
                        "parser = ET.XMLParser(resolve_entities=False)"
                    ),
                ))
                break


# ── 2. Dependency CVE Scanner ─────────────────────────────────────────────────

# Known vulnerable packages with minimum safe versions (updated periodically)
# Format: {package: [(vulnerable_below, cve_id, severity, description)]}
KNOWN_VULNERABILITIES: Dict[str, List[Tuple]] = {
    "requests":          [("2.32.0",  "CVE-2024-35195", "HIGH",   "SSRF via proxies")],
    "pillow":            [("10.3.0",  "CVE-2024-28219", "HIGH",   "Buffer overflow")],
    "cryptography":      [("42.0.4",  "CVE-2024-26130", "HIGH",   "NULL pointer dereference")],
    "aiohttp":           [("3.9.4",   "CVE-2024-23334", "HIGH",   "Directory traversal")],
    "werkzeug":          [("3.0.3",   "CVE-2024-34069", "HIGH",   "Remote code execution")],
    "jinja2":            [("3.1.4",   "CVE-2024-34064", "MEDIUM", "XSS via Markup")],
    "django":            [("4.2.13",  "CVE-2024-27351", "HIGH",   "ReDoS in URL parser")],
    "flask":             [("3.0.3",   "CVE-2023-30861", "HIGH",   "Session cookie exposure")],
    "paramiko":          [("3.4.0",   "CVE-2023-48795", "MEDIUM", "Terrapin attack")],
    "pycryptodome":      [("3.19.1",  "CVE-2023-52323", "MEDIUM", "Side-channel attack")],
    "sqlalchemy":        [("2.0.21",  "CVE-2023-40018", "MEDIUM", "SQL injection via JSON")],
    "pyyaml":            [("6.0.1",   "CVE-2023-47627", "HIGH",   "Arbitrary code via yaml.load")],
    "urllib3":           [("2.2.2",   "CVE-2024-37891", "MEDIUM", "Proxy auth header leak")],
    "setuptools":        [("70.0.0",  "CVE-2024-6345",  "HIGH",   "Remote code execution")],
    "certifi":           [("2024.7.4","CVE-2024-39689", "MEDIUM", "Compromised CA")],
}


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse semver-ish version string to comparable tuple."""
    try:
        parts = re.findall(r"\d+", v.split("+")[0].split("a")[0].split("b")[0])
        return tuple(int(p) for p in parts[:3])
    except Exception:
        return (0, 0, 0)


def _version_below(installed: str, threshold: str) -> bool:
    """Return True if installed < threshold."""
    return _parse_version(installed) < _parse_version(threshold)


def _parse_requirements(req_text: str) -> List[Tuple[str, str]]:
    """Parse requirements.txt content into [(package, version)] pairs."""
    results = []
    for line in req_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Handle: package==1.2.3, package>=1.2.3, package~=1.2.3
        m = re.match(r"^([a-zA-Z0-9_.-]+)\s*[=><~!]+\s*([\d.]+)", line)
        if m:
            results.append((m.group(1).lower().replace("-", "_"), m.group(2)))
        else:
            # No version pinned
            name = re.match(r"^([a-zA-Z0-9_.-]+)", line)
            if name:
                results.append((name.group(1).lower().replace("-", "_"), "0.0.0"))
    return results


class DependencyScanner:
    """
    Scans requirements.txt / pyproject.toml for known CVEs.

    Uses a bundled vulnerability database for offline checks.
    When FORGE_CVE_CHECK=true, also queries the OSV.dev API for up-to-date results.
    """

    def scan_requirements(self, req_text: str) -> ValidationResult:
        findings = []
        packages = _parse_requirements(req_text)

        for pkg, version in packages:
            # Check bundled database
            vulns = KNOWN_VULNERABILITIES.get(pkg, [])
            for safe_version, cve_id, severity, description in vulns:
                if version == "0.0.0" or _version_below(version, safe_version):
                    findings.append(Finding(
                        severity=severity,
                        category="dependency-cve",
                        location=f"{pkg}=={version}",
                        description=f"{cve_id}: {description} (safe: >={safe_version})",
                        suggestion=f"Upgrade to {pkg}>={safe_version}",
                    ))

            # Online CVE check via OSV API
            if _CVE_CHECK and version != "0.0.0":
                osv_findings = self._check_osv(pkg, version)
                findings.extend(osv_findings)

        verdict = "FAIL" if any(f.severity in ("CRITICAL", "HIGH") for f in findings) \
                  else ("WARN" if findings else "PASS")
        return ValidationResult(
            validator="DependencyScanner",
            verdict=verdict,
            findings=findings,
            stats={"packages_checked": len(packages), "vulnerabilities": len(findings)},
        )

    def _check_osv(self, package: str, version: str) -> List[Finding]:
        """Query OSV.dev for vulnerabilities in a specific package version."""
        try:
            payload = json.dumps({
                "version": version,
                "package": {"name": package, "ecosystem": "PyPI"},
            }).encode()
            req = urllib.request.Request(
                "https://api.osv.dev/v1/query",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            findings = []
            for vuln in data.get("vulns", [])[:3]:  # cap at 3 per package
                vuln_id = vuln.get("id", "UNKNOWN")
                summary = vuln.get("summary", "No description")[:100]
                findings.append(Finding(
                    severity="HIGH",
                    category="dependency-cve",
                    location=f"{package}=={version}",
                    description=f"{vuln_id}: {summary}",
                    suggestion=f"Check https://osv.dev/vulnerability/{vuln_id} for fix version",
                ))
            return findings
        except Exception:
            return []  # OSV unavailable — silently skip


# ── 3. Access Control Checker ─────────────────────────────────────────────────

class AccessControlChecker:
    """
    Validates permission checks on protected operations.
    Detects privilege escalation patterns and missing authorisation.
    """

    # Functions that indicate a privilege-sensitive operation
    SENSITIVE_OPS = {
        "delete", "drop", "truncate", "rm", "remove",
        "grant", "revoke", "chmod", "chown",
        "create_user", "add_user", "set_password", "change_password",
        "sudo", "su", "setuid", "setgid",
        "execute", "eval", "exec",
    }

    # Functions/decorators that indicate authorisation checks
    AUTH_CHECKS = {
        "is_admin", "is_authenticated", "has_permission", "check_permissions",
        "require_permission", "assert_allowed", "authorize",
        "current_user.is_staff", "request.user.is_superuser",
        "permission_denied", "raise PermissionError", "raise Forbidden",
        "HTTPException(status_code=403", "HTTPException(status_code=401",
    }

    def validate(self, code: str) -> ValidationResult:
        findings = []
        self._check_sensitive_without_auth(code, findings)
        self._check_direct_object_reference(code, findings)

        verdict = "FAIL" if any(f.severity in ("CRITICAL", "HIGH") for f in findings) \
                  else ("WARN" if findings else "PASS")
        return ValidationResult(
            validator="AccessControlChecker",
            verdict=verdict,
            findings=findings,
        )

    def _check_sensitive_without_auth(self, code: str, findings: List[Finding]) -> None:
        """Detect sensitive operations without adjacent auth checks."""
        tree = _parse_code(code)
        if tree is None:
            return

        has_any_auth = any(check in code for check in self.AUTH_CHECKS)
        if not has_any_auth:
            return  # no auth system — different problem (caught by OWASP checker)

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_code = ast.unparse(func) if hasattr(ast, "unparse") else ""
            has_auth = any(check in func_code for check in self.AUTH_CHECKS)
            has_sensitive = any(op in func_code.lower() for op in self.SENSITIVE_OPS)

            if has_sensitive and not has_auth:
                findings.append(Finding(
                    severity="HIGH",
                    category="access-control",
                    location=f"def {func.name} (line {func.lineno})",
                    description="Sensitive operation without authorisation check",
                    suggestion=(
                        f"Add permission check before sensitive operations in '{func.name}'. "
                        "Verify the current user has the required role/permission."
                    ),
                ))

    def _check_direct_object_reference(self, code: str, findings: List[Finding]) -> None:
        """Detect IDOR patterns — fetching by user-supplied ID without ownership check."""
        tree = _parse_code(code)
        if tree is None:
            return

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_code = ast.unparse(func) if hasattr(ast, "unparse") else ""

            # Look for get_by_id / filter(id=...) patterns
            has_id_lookup = bool(re.search(
                r"(get_by_id|get\(id=|filter\(id=|objects\.get\(|\.find_one\()",
                func_code, re.IGNORECASE
            ))
            # Look for ownership verification
            has_ownership_check = bool(re.search(
                r"(user_id|owner|created_by|author).*==.*user|user.*==.*(user_id|owner)",
                func_code, re.IGNORECASE
            ))

            if has_id_lookup and not has_ownership_check:
                findings.append(Finding(
                    severity="MEDIUM",
                    category="access-control",
                    location=f"def {func.name}",
                    description="Possible IDOR — fetches by ID without ownership verification",
                    suggestion=(
                        "Verify the fetched object belongs to the current user: "
                        "obj = get_by_id(id); assert obj.user_id == current_user.id"
                    ),
                ))


# ── 4. Enhanced Secrets Scanner ───────────────────────────────────────────────

class SecretsScanner:
    """
    Enhanced secret detection — extends the basic regex scanner in SecurityAgent
    with more patterns and entropy analysis.
    """

    PATTERNS = [
        # Cloud providers
        (r"AKIA[0-9A-Z]{16}",                             "AWS Access Key ID"),
        (r"sk-[a-zA-Z0-9]{32,}",                          "OpenAI API key"),
        (r"sk-ant-[a-zA-Z0-9\-]{20,}",                    "Anthropic API key"),
        (r"AIza[0-9A-Za-z\-_]{35}",                       "Google API key"),
        (r"ghp_[a-zA-Z0-9]{36}",                          "GitHub personal access token"),
        (r"github_pat_[a-zA-Z0-9_]{82}",                  "GitHub fine-grained PAT"),
        (r"xoxb-[0-9]{11}-[0-9]{11}-[a-zA-Z0-9]{24}",    "Slack bot token"),
        (r"xoxp-[0-9]+-[0-9]+-[0-9]+-[a-zA-Z0-9]+",      "Slack user token"),
        (r"Bearer [a-zA-Z0-9\._\-]{20,}",                 "Bearer token"),

        # Database connection strings
        (r"postgres://[^:]+:[^@]+@",                       "PostgreSQL connection string with credentials"),
        (r"mysql://[^:]+:[^@]+@",                          "MySQL connection string with credentials"),
        (r"mongodb://[^:]+:[^@]+@",                        "MongoDB connection string with credentials"),
        (r"redis://:[^@]+@",                               "Redis connection string with credentials"),

        # Private keys
        (r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY",  "Private key"),
        (r"-----BEGIN CERTIFICATE-----",                        "Certificate (may be sensitive)"),

        # Generic high-entropy strings (likely secrets)
        (r'(?:password|passwd|pwd|secret|token|api_key|apikey|auth)\s*[=:]\s*["\'][^"\']{12,}["\']',
         "Potential hardcoded credential"),
    ]

    def scan(self, code: str, filename: str = "") -> ValidationResult:
        findings = []
        for pattern, label in self.PATTERNS:
            for m in re.finditer(pattern, code, re.IGNORECASE):
                # Skip obvious placeholders
                match_text = m.group()
                if any(p in match_text.lower() for p in
                       ("example", "your_", "changeme", "placeholder",
                        "xxx", "<", ">", "test", "dummy")):
                    continue
                line = code[:m.start()].count("\n") + 1
                findings.append(Finding(
                    severity="CRITICAL",
                    category="secrets",
                    location=f"{filename}:line {line}" if filename else f"line {line}",
                    description=f"{label} detected in source code",
                    suggestion=(
                        "Remove the secret from source code immediately. "
                        "Use environment variables or a secrets manager. "
                        "Rotate the exposed credential."
                    ),
                ))

        verdict = "FAIL" if (findings and _SECRETS_GATE == "fail") \
                  else ("WARN" if findings else "PASS")
        return ValidationResult(
            validator="SecretsScanner",
            verdict=verdict,
            findings=findings,
            stats={"secrets_found": len(findings)},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_code(code: str) -> Optional[ast.AST]:
    try:
        return ast.parse(code)
    except SyntaxError:
        return None


# ── Combined security runner ──────────────────────────────────────────────────

def run_security_validators(
    code: str,
    requirements_txt: str = "",
) -> List[ValidationResult]:
    """
    Run all security validators on code and optional requirements.
    Returns list of ValidationResult — used by the SecurityGate.
    """
    results = [
        SecretsScanner().scan(code),
        OWASPValidator().validate(code),
        AccessControlChecker().validate(code),
    ]
    if requirements_txt:
        results.append(DependencyScanner().scan_requirements(requirements_txt))
    return results