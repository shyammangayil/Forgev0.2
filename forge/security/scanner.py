"""
FORGE Security Module
=====================
Secret scanning, input validation, and audit support.
"""
from __future__ import annotations

import re
from typing import List, Tuple


# Known secret patterns — extend as needed
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{32,}", "OpenAI API key"),
    (r"sk-ant-[A-Za-z0-9\-]{20,}", "Anthropic API key"),
    (r"AIza[A-Za-z0-9\-_]{35}", "Google API key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"glpat-[A-Za-z0-9\-_]{20}", "GitLab PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"(?i)password\s*=\s*['\"][^'\"]{8,}", "Hardcoded password"),
    (r"(?i)api[_-]?key\s*=\s*['\"][A-Za-z0-9+/\-_]{16,}", "Generic API key"),
    (r"(?i)secret\s*=\s*['\"][^'\"]{8,}", "Hardcoded secret"),
]

# Prompt injection patterns
INJECTION_PATTERNS = [
    r"ignore previous instructions",
    r"disregard.*instructions",
    r"you are now",
    r"pretend you are",
    r"act as if",
    r"jailbreak",
    r"DAN mode",
]


def scan_for_secrets(content: str) -> List[Tuple[str, str]]:
    """
    Scan content for hardcoded secrets.
    Returns list of (pattern_description, matched_excerpt) tuples.
    """
    findings = []
    for pattern, description in SECRET_PATTERNS:
        matches = re.findall(pattern, content)
        for match in matches:
            # Redact middle characters for safety
            redacted = match[:6] + "***" + match[-4:] if len(match) > 10 else "***"
            findings.append((description, redacted))
    return findings


def detect_prompt_injection(content: str) -> List[str]:
    """Detect prompt injection attempts in user-supplied content."""
    findings = []
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(f"Injection pattern: {pattern}")
    return findings


def sanitize_task_input(title: str, description: str) -> Tuple[str, str, List[str]]:
    """
    Sanitize task inputs. Returns (clean_title, clean_description, warnings).
    """
    warnings = []

    # Detect injection in description
    injections = detect_prompt_injection(description)
    if injections:
        warnings.extend(injections)

    # Detect secrets in description
    secrets = scan_for_secrets(description)
    if secrets:
        for desc, _ in secrets:
            warnings.append(f"Possible secret in task description: {desc}")

    # Basic sanitization
    clean_title = title.strip()[:200]
    clean_description = description.strip()[:50000]

    return clean_title, clean_description, warnings


def redact_secrets_from_log(text: str) -> str:
    """Redact known secret patterns before logging."""
    for pattern, _ in SECRET_PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text)
    return text
