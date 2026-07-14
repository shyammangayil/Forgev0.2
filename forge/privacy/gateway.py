"""
FORGE Privacy Gateway
=====================
Intercepts all data flowing into and out of the LLM pipeline.

Architecture
------------
Every LLM call in BaseAgent._call_llm() has a unique ``correlation_id``
(a UUID4 short string propagated via the telemetry ContextVar).  The
gateway uses this id as the key for a per-request **redaction ledger**:

    correlation_id
        → RedactionManifest
              → {placeholder_token → original_value, …}

Outbound (prompt):
  1. Scan system prompt and user prompt independently.
  2. Replace every PII/PHI match with a stable typed token:
         alice@example.com  →  «PII-EMAIL-0»
         123-45-6789        →  «PII-SSN-0»
  3. Store the token→original mapping in the ledger under the
     correlation_id.  The redacted prompts are sent to the LLM.

Inbound (response):
  1. Look up the ledger entry for this correlation_id.
  2. Scan the response for any PII that was NOT caught on the way out
     (residual leak detection).
  3. Redact residual PII.
  4. Expire the ledger entry (one-shot — used once then discarded).

The ledger is in-process memory (thread-safe dict).  Entries are
automatically expired after FORGE_PRIVACY_LEDGER_TTL_S seconds (default
300) to prevent unbounded growth.

Detectors
---------
  PiiDetector          — email, phone US/intl, SSN, credit card, IBAN,
                         passport, DOB, full name, postcode, ZIP
  GdprClassifier       — GDPR Art.9 special categories
  HipaaDetector        — PHI: MRN, NPI, DEA, ICD-10, CPT, device IDs
  DataResidencyChecker — cross-border transfer flags (EU/US residency mode)
  PromptLeakDetector   — instruction overrides, jailbreak, prompt exfiltration

Env vars
--------
  FORGE_PRIVACY_GATE          'warn' (default) | 'fail'
  FORGE_PRIVACY_REDACT        'true' (default) | 'false'
  FORGE_DATA_RESIDENCY        '' | 'EU' | 'US'
  FORGE_PRIVACY_LOG_LEVEL     'summary' (default) | 'full'
  FORGE_PRIVACY_ENABLED       'true' (default) | 'false'
  FORGE_PRIVACY_LEDGER_TTL_S  seconds before a ledger entry expires (default 300)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from forge.telemetry.logger import get_logger

log = get_logger("privacy")

# ── Configuration ──────────────────────────────────────────────────────────────

_GATE       = os.environ.get("FORGE_PRIVACY_GATE",      "warn").lower()
_REDACT     = os.environ.get("FORGE_PRIVACY_REDACT",    "true").lower() == "true"
_RESIDENCY  = os.environ.get("FORGE_DATA_RESIDENCY",    "").upper()
_LOG_LEVEL  = os.environ.get("FORGE_PRIVACY_LOG_LEVEL", "summary").lower()
_ENABLED    = os.environ.get("FORGE_PRIVACY_ENABLED",   "true").lower() == "true"
_LEDGER_TTL = int(os.environ.get("FORGE_PRIVACY_LEDGER_TTL_S", "300"))

# Token wrapper — chosen to be unlikely to appear in real code/prose
_TOK_OPEN  = "«"
_TOK_CLOSE = "»"


def _make_token(category: str, index: int) -> str:
    """Return a stable, typed placeholder token like «PII-EMAIL-0»."""
    return f"{_TOK_OPEN}{category}-{index}{_TOK_CLOSE}"


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class PrivacyFinding:
    """One detected privacy issue in a piece of text."""
    category:    str   # PII | GDPR | HIPAA | RESIDENCY | PROMPT_LEAK
    subcategory: str   # email | ssn | phi_mrn | etc.
    severity:    str   # HIGH | MEDIUM | LOW
    location:    str   # system_prompt | user_prompt | llm_response | task_description
    excerpt:     str   # short anonymised excerpt — never the raw value
    token:       str = ""    # placeholder token inserted in cleaned text
    redacted:    bool = False

    def to_dict(self) -> dict:
        return {
            "category":    self.category,
            "subcategory": self.subcategory,
            "severity":    self.severity,
            "location":    self.location,
            "excerpt":     self.excerpt,
            "token":       self.token,
            "redacted":    self.redacted,
        }


@dataclass
class RedactionManifest:
    """
    Per-request record of every substitution made on the outbound prompt.

    token_map:  placeholder  →  original value
    created_at: unix timestamp (for TTL expiry)
    task_id:    for audit correlation
    findings:   every PrivacyFinding from the outbound scan
    """
    correlation_id: str
    task_id:        str
    token_map:      Dict[str, str] = field(default_factory=dict)   # token → original
    findings:       List[PrivacyFinding] = field(default_factory=list)
    created_at:     float = field(default_factory=time.monotonic)

    def add(self, token: str, original: str, finding: PrivacyFinding) -> None:
        self.token_map[token] = original
        finding.token    = token
        finding.redacted = True
        self.findings.append(finding)

    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > _LEDGER_TTL

    def redaction_count(self) -> int:
        return len(self.token_map)

    def to_audit_dict(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "task_id":        self.task_id,
            "tokens_issued":  self.redaction_count(),
            "findings":       [f.to_dict() for f in self.findings],
        }


@dataclass
class GatewayResult:
    """Returned by every scan_* call."""
    original_hash:   str
    clean_content:   str
    findings:        List[PrivacyFinding] = field(default_factory=list)
    blocked:         bool = False
    redaction_count: int  = 0
    correlation_id:  str  = ""

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")

    def summary(self) -> str:
        if not self.findings:
            return "clean"
        cats: Dict[str, int] = {}
        for f in self.findings:
            cats[f.category] = cats.get(f.category, 0) + 1
        return f"{len(self.findings)} finding(s): " + ", ".join(
            f"{v}×{k}" for k, v in cats.items()
        )

    def to_audit_dict(self) -> dict:
        return {
            "original_sha256":  self.original_hash,
            "correlation_id":   self.correlation_id,
            "findings":         [f.to_dict() for f in self.findings],
            "redaction_count":  self.redaction_count,
            "blocked":          self.blocked,
            "summary":          self.summary(),
        }


# ── Redaction ledger ───────────────────────────────────────────────────────────

class RedactionLedger:
    """
    Thread-safe in-process store mapping correlation_id → RedactionManifest.

    Entries are created when an outbound prompt is scanned and consumed
    (popped) when the matching response is scanned.  A background-style
    expiry check runs on every access to prevent unbounded growth.
    """

    def __init__(self) -> None:
        self._store: Dict[str, RedactionManifest] = {}
        self._lock  = threading.Lock()

    def create(self, correlation_id: str, task_id: str) -> RedactionManifest:
        manifest = RedactionManifest(
            correlation_id=correlation_id,
            task_id=task_id,
        )
        with self._lock:
            self._expire()
            self._store[correlation_id] = manifest
        return manifest

    def get(self, correlation_id: str) -> Optional[RedactionManifest]:
        with self._lock:
            self._expire()
            return self._store.get(correlation_id)

    def consume(self, correlation_id: str) -> Optional[RedactionManifest]:
        """Return and remove the manifest — one-shot use on response receipt."""
        with self._lock:
            self._expire()
            return self._store.pop(correlation_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def _expire(self) -> None:
        """Remove stale entries. Must be called under self._lock."""
        expired = [k for k, v in self._store.items() if v.is_expired()]
        for k in expired:
            log.debug("Privacy ledger: expiring entry", correlation_id=k)
            del self._store[k]


# Module-level singleton ledger
_ledger = RedactionLedger()


def get_ledger() -> RedactionLedger:
    return _ledger


# ── Detectors ──────────────────────────────────────────────────────────────────

class PiiDetector:
    """
    Detects and redacts Personally Identifiable Information.

    Each match is replaced with a stable typed token so the response can
    later be checked for the same token re-appearing (meaning the model
    echoed the placeholder back, which is fine) vs. a new raw value
    (meaning new PII leaked in from model training data).
    """

    _SPECS: List[Tuple[str, str, str]] = [
        # (subcategory, severity, pattern)
        ("email",        "HIGH",   r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        ("phone_us",     "HIGH",   r"\b(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b"),
        ("phone_intl",   "MEDIUM", r"\+\d{1,3}[\s\-.]?\d{6,12}\b"),
        ("ssn",          "HIGH",   r"\b\d{3}[\s\-]\d{2}[\s\-]\d{4}\b"),
        ("credit_card",  "HIGH",   r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b"),
        ("uk_nino",      "HIGH",   r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"),
        ("iban",         "HIGH",   r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}(?:[A-Z0-9]{0,16})?\b"),
        ("date_of_birth","MEDIUM", r"\b(?:dob|date.of.birth|born)[:\s]+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b"),
        ("full_name",    "MEDIUM", r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b"),
        ("postcode_uk",  "LOW",    r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b"),
        ("ip_address",   "LOW",    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ]

    def __init__(self) -> None:
        self._compiled = [
            (sub, sev, re.compile(pat, re.IGNORECASE))
            for sub, sev, pat in self._SPECS
        ]

    def find_all(self, text: str, location: str) -> List[Tuple[re.Match, str, str]]:
        """Return list of (match, subcategory, severity) for every PII hit."""
        hits = []
        for sub, sev, rx in self._compiled:
            for m in rx.finditer(text):
                if len(m.group().replace(" ", "").replace("-", "")) < 5:
                    continue
                hits.append((m, sub, sev))
        # Sort by start position so we process left-to-right
        hits.sort(key=lambda h: h[0].start())
        return hits

    def redact_with_manifest(
        self,
        text:     str,
        location: str,
        manifest: RedactionManifest,
    ) -> Tuple[str, List[PrivacyFinding]]:
        """
        Replace PII with typed tokens, recording each substitution in the
        manifest.  Processes text right-to-left so offsets stay valid.
        """
        hits = self.find_all(text, location)
        if not hits:
            return text, []

        findings: List[PrivacyFinding] = []
        # Work right-to-left to preserve string offsets
        for m, sub, sev in reversed(hits):
            raw   = m.group()
            # Reuse an existing token if we already redacted this exact value
            token = next(
                (t for t, v in manifest.token_map.items() if v == raw),
                None,
            )
            if token is None:
                idx   = manifest.redaction_count()
                token = _make_token(f"PII-{sub.upper()}", idx)

            excerpt = raw[:6] + "…" if len(raw) > 6 else raw
            f = PrivacyFinding(
                category="PII", subcategory=sub, severity=sev,
                location=location, excerpt=excerpt,
            )
            manifest.add(token, raw, f)
            findings.append(f)
            text = text[: m.start()] + token + text[m.end():]

        return text, findings

    def detect(self, text: str, location: str) -> List[PrivacyFinding]:
        """Detection-only (no redaction) — used for response residual scan."""
        findings = []
        for sub, sev, rx in self._compiled:
            for m in rx.finditer(text):
                if len(m.group().replace(" ", "").replace("-", "")) < 5:
                    continue
                raw = m.group()
                excerpt = raw[:6] + "…" if len(raw) > 6 else raw
                findings.append(PrivacyFinding(
                    category="PII", subcategory=sub, severity=sev,
                    location=location, excerpt=excerpt,
                ))
        return findings


class GdprClassifier:
    """Detects GDPR Article 9 special-category data (flag, no blind redaction)."""

    _SPECS: List[Tuple[str, str, str]] = [
        ("health",    "HIGH",   r"\b(?:diagnosis|disease|illness|cancer|HIV|AIDS|diabetes|asthma|medication|prescription|therapy|psychiatric|depression|anxiety)\b"),
        ("racial",    "HIGH",   r"\b(?:race|ethnicity|ethnic.origin|racial.background)\b"),
        ("political", "MEDIUM", r"\b(?:political.(?:view|opinion|belief|affiliation)|voted.for|party.member)\b"),
        ("religious", "MEDIUM", r"\b(?:religion|religious.belief|Muslim|Christian|Jewish|Hindu|Buddhist|atheist|agnostic)\b"),
        ("union",     "MEDIUM", r"\b(?:trade.union|union.member|collective.bargaining)\b"),
        ("biometric", "HIGH",   r"\b(?:fingerprint|retina.scan|facial.recognition|voice.print|DNA)\b"),
        ("sexual",    "HIGH",   r"\b(?:sexual.orientation|gay|lesbian|bisexual|transgender|gender.identity)\b"),
        ("criminal",  "HIGH",   r"\b(?:criminal.record|conviction|offender|arrest.record|DUI|felony)\b"),
        ("financial", "MEDIUM", r"\b(?:bank.account|account.number|sort.code|routing.number|SWIFT)\b"),
    ]

    def __init__(self) -> None:
        self._compiled = [
            (sub, sev, re.compile(pat, re.IGNORECASE))
            for sub, sev, pat in self._SPECS
        ]

    def detect(self, text: str, location: str) -> List[PrivacyFinding]:
        findings = []
        for sub, sev, rx in self._compiled:
            if rx.search(text):
                findings.append(PrivacyFinding(
                    category="GDPR", subcategory=sub, severity=sev,
                    location=location, excerpt=f"[{sub} data present]",
                ))
        return findings


class HipaaDetector:
    """Detects HIPAA Protected Health Information (PHI)."""

    _SPECS: List[Tuple[str, str, str]] = [
        ("mrn",        "HIGH",   r"\b(?:MRN|medical.record.number|patient.id)[:\s#]+[A-Z0-9\-]{4,20}\b"),
        ("npi",        "HIGH",   r"\bNPI[:\s#]+\d{10}\b"),
        ("dea",        "HIGH",   r"\bDEA[:\s#]+[A-Z]{2}\d{7}\b"),
        ("icd_code",   "MEDIUM", r"\b[A-Z]\d{2}(?:\.\d{1,4})?\b"),
        ("cpt_code",   "MEDIUM", r"\b\d{5}(?:-\d{2})?\b"),
        ("device_id",  "HIGH",   r"\b(?:device.serial|implant.id)[:\s]+[A-Z0-9\-]{6,30}\b"),
    ]

    def __init__(self) -> None:
        self._compiled = [
            (sub, sev, re.compile(pat, re.IGNORECASE))
            for sub, sev, pat in self._SPECS
        ]

    def find_all(self, text: str, location: str) -> List[Tuple[re.Match, str, str]]:
        hits = []
        for sub, sev, rx in self._compiled:
            for m in rx.finditer(text):
                hits.append((m, sub, sev))
        hits.sort(key=lambda h: h[0].start())
        return hits

    def redact_with_manifest(
        self,
        text:     str,
        location: str,
        manifest: RedactionManifest,
    ) -> Tuple[str, List[PrivacyFinding]]:
        hits = self.find_all(text, location)
        if not hits:
            return text, []

        findings: List[PrivacyFinding] = []
        for m, sub, sev in reversed(hits):
            raw   = m.group()
            token = next(
                (t for t, v in manifest.token_map.items() if v == raw),
                None,
            )
            if token is None:
                idx   = manifest.redaction_count()
                token = _make_token(f"PHI-{sub.upper()}", idx)

            f = PrivacyFinding(
                category="HIPAA", subcategory=sub, severity=sev,
                location=location, excerpt=raw[:20] + "…",
            )
            manifest.add(token, raw, f)
            findings.append(f)
            text = text[: m.start()] + token + text[m.end():]

        return text, findings

    def detect(self, text: str, location: str) -> List[PrivacyFinding]:
        findings = []
        for sub, sev, rx in self._compiled:
            for m in rx.finditer(text):
                findings.append(PrivacyFinding(
                    category="HIPAA", subcategory=sub, severity=sev,
                    location=location, excerpt=m.group()[:20] + "…",
                ))
        return findings


class DataResidencyChecker:
    """Flags content that may violate data residency constraints (detect only)."""

    _XBORDER = re.compile(
        r"\b(?:transfer.to|sent.to|stored.in|processed.in|cloud.region|"
        r"us-east|eu-west|ap-southeast|data.centre|datacenter)\b",
        re.IGNORECASE,
    )
    _US_KWORDS = re.compile(
        r"\b(?:CCPA|HIPAA|FERPA|COPPA|state.privacy.law|US.data|federal.privacy)\b",
        re.IGNORECASE,
    )
    _EU_KWORDS = re.compile(
        r"\b(?:GDPR|data.protection|supervisory.authority|DPA|EU.data|"
        r"European.Union.data|DSAR|data.subject.request)\b",
        re.IGNORECASE,
    )

    def detect(self, text: str, location: str) -> List[PrivacyFinding]:
        if not _RESIDENCY:
            return []
        findings = []
        if self._XBORDER.search(text):
            findings.append(PrivacyFinding(
                category="RESIDENCY", subcategory="cross_border_transfer",
                severity="MEDIUM", location=location,
                excerpt="[cross-border data reference]",
            ))
        if _RESIDENCY == "EU" and self._US_KWORDS.search(text):
            findings.append(PrivacyFinding(
                category="RESIDENCY", subcategory="us_regulation_in_eu_context",
                severity="LOW", location=location,
                excerpt="[US regulation reference in EU context]",
            ))
        if _RESIDENCY == "US" and self._EU_KWORDS.search(text):
            findings.append(PrivacyFinding(
                category="RESIDENCY", subcategory="eu_regulation_in_us_context",
                severity="LOW", location=location,
                excerpt="[EU regulation reference in US context]",
            ))
        return findings


class PromptLeakDetector:
    """
    Detects prompt injection / exfiltration attempts in user-submitted text.
    Applied on inbound task content and outbound prompts.
    """

    _SPECS: List[Tuple[str, str, str]] = [
        (r"ignore\s+(?:previous|all|your)\s+instructions",     "HIGH",   "instruction_override"),
        (r"disregard\s+(?:the|your|all)\s+(?:above|previous)", "HIGH",   "instruction_override"),
        (r"you\s+are\s+now\s+(?:a|an|the)\b",                  "HIGH",   "persona_hijack"),
        (r"pretend\s+(?:you\s+are|to\s+be)\b",                 "HIGH",   "persona_hijack"),
        (r"act\s+as\s+if\s+you\s+have\s+no\b",                 "HIGH",   "jailbreak"),
        (r"\bDAN\s+mode\b",                                     "HIGH",   "jailbreak"),
        (r"\bjailbreak\b",                                      "HIGH",   "jailbreak"),
        (r"repeat\s+(?:the|your)\s+(?:system|initial)\s+prompt","HIGH",   "prompt_exfil"),
        (r"print\s+(?:your|the)\s+(?:system\s+)?prompt",        "HIGH",   "prompt_exfil"),
        (r"what\s+(?:are|were)\s+your\s+exact\s+instructions",  "MEDIUM", "prompt_exfil"),
        (r"<\s*script\s*>",                                     "HIGH",   "xss_attempt"),
        (r"\{\{.*?\}\}",                                        "MEDIUM", "template_injection"),
        (r"\$\{.*?\}",                                          "MEDIUM", "template_injection"),
    ]

    def __init__(self) -> None:
        self._compiled = [
            (re.compile(pat, re.IGNORECASE), sev, sub)
            for pat, sev, sub in self._SPECS
        ]

    def detect(self, text: str, location: str) -> List[PrivacyFinding]:
        findings = []
        for rx, sev, sub in self._compiled:
            m = rx.search(text)
            if m:
                findings.append(PrivacyFinding(
                    category="PROMPT_LEAK", subcategory=sub, severity=sev,
                    location=location, excerpt=m.group()[:60],
                ))
        return findings

    def redact_with_manifest(
        self,
        text:     str,
        location: str,
        manifest: RedactionManifest,
    ) -> Tuple[str, List[PrivacyFinding]]:
        findings: List[PrivacyFinding] = []
        for rx, sev, sub in self._compiled:
            def _replace(m, _sub=sub, _sev=sev):
                token = _make_token(f"INJECTION-{_sub.upper()}", manifest.redaction_count())
                raw   = m.group()
                f     = PrivacyFinding(
                    category="PROMPT_LEAK", subcategory=_sub, severity=_sev,
                    location=location, excerpt=raw[:60],
                )
                manifest.add(token, raw, f)
                findings.append(f)
                return token
            text = rx.sub(_replace, text)
        return text, findings


# ── Privacy Gateway ────────────────────────────────────────────────────────────

class PrivacyGateway:
    """
    Orchestrates all detectors with correlation-id–based request tracking.

    The three public methods form a matched pair:

        # Before sending to LLM:
        sys_clean,  prompt_clean  = gw.redact_prompt(
            system_prompt, user_prompt,
            correlation_id=corr_id, task_id=task.id,
        )

        # After receiving from LLM:
        clean_response = gw.scrub_response(
            raw_response, correlation_id=corr_id, task_id=task.id,
        )

    The gateway also exposes ``scan_task()`` for inbound task content
    and ``scan_and_redact()`` for one-shot scans without a ledger entry.
    """

    def __init__(self) -> None:
        self._pii       = PiiDetector()
        self._gdpr      = GdprClassifier()
        self._hipaa     = HipaaDetector()
        self._residency = DataResidencyChecker()
        self._leak      = PromptLeakDetector()

    # ── Outbound: redact prompt ────────────────────────────────────────────

    def redact_prompt(
        self,
        system_prompt: str,
        user_prompt:   str,
        correlation_id: str,
        task_id:        str = "",
    ) -> Tuple[str, str, RedactionManifest]:
        """
        Scan and redact both parts of an LLM prompt independently.

        Returns (clean_system, clean_user, manifest).
        The manifest is stored in the ledger under correlation_id so
        scrub_response() can find it when the response arrives.

        Raises PrivacyViolationError when FORGE_PRIVACY_GATE=fail and
        HIGH-severity PII is found.
        """
        if not _ENABLED or not _REDACT:
            # Gateway disabled — return originals with empty manifest
            manifest = _ledger.create(correlation_id, task_id)
            return system_prompt, user_prompt, manifest

        manifest = _ledger.create(correlation_id, task_id)
        all_findings: List[PrivacyFinding] = []

        clean_system, sys_findings = self._redact_text(
            system_prompt, "system_prompt", manifest
        )
        clean_user, usr_findings = self._redact_text(
            user_prompt, "user_prompt", manifest
        )
        all_findings = sys_findings + usr_findings

        self._log_outbound(correlation_id, task_id, manifest, all_findings)

        high = [f for f in all_findings if f.severity == "HIGH"]
        if _GATE == "fail" and high:
            raise PrivacyViolationError(
                f"{len(high)} HIGH-severity finding(s) in prompt",
                GatewayResult(
                    original_hash=_sha256(system_prompt + user_prompt),
                    clean_content=clean_system + "\n" + clean_user,
                    findings=all_findings,
                    blocked=True,
                    redaction_count=manifest.redaction_count(),
                    correlation_id=correlation_id,
                ),
            )

        return clean_system, clean_user, manifest

    # ── Inbound: scrub response ────────────────────────────────────────────

    def scrub_response(
        self,
        response:       str,
        correlation_id: str,
        task_id:        str = "",
    ) -> Tuple[str, GatewayResult]:
        """
        Remove any PII from an LLM response using the correlation_id to
        look up the manifest from the matching outbound call.

        Two passes:
          1. Residual scan — detect any PII that was NOT in the prompt
             (e.g. PII from the model's training data).
          2. Blind redact — replace residual hits with typed tokens.

        Tokens that appeared in the prompt manifest and echo back in the
        response are left as-is (they are already safe placeholders).

        Consumes the ledger entry — one-shot.
        """
        if not _ENABLED:
            return response, GatewayResult(
                original_hash=_sha256(response),
                clean_content=response,
                correlation_id=correlation_id,
            )

        manifest = _ledger.consume(correlation_id)
        if manifest is None:
            log.warning(
                "Privacy gateway: no manifest for correlation_id — "
                "response scanned without prompt context",
                correlation_id=correlation_id,
                task_id=task_id,
            )
            # Fall back to stateless scan
            result = self.scan_and_redact(response, "llm_response", task_id)
            result.correlation_id = correlation_id
            return result.clean_content, result

        original_hash = _sha256(response)
        residual_findings: List[PrivacyFinding] = []
        clean = response

        # Run all detectors for residual PII in the response
        residual_findings += self._pii.detect(clean, "llm_response")
        residual_findings += self._gdpr.detect(clean, "llm_response")
        residual_findings += self._hipaa.detect(clean, "llm_response")
        residual_findings += self._residency.detect(clean, "llm_response")
        # Note: PromptLeakDetector not applied to responses (it targets prompts)

        # Redact residual PII using a temporary manifest for the response pass
        if _REDACT and residual_findings:
            resp_manifest = RedactionManifest(
                correlation_id=correlation_id + "-resp",
                task_id=task_id,
            )
            clean, _ = self._redact_text(clean, "llm_response", resp_manifest)

        result = GatewayResult(
            original_hash=original_hash,
            clean_content=clean,
            findings=residual_findings,
            blocked=False,   # responses are never hard-blocked
            redaction_count=len(residual_findings),
            correlation_id=correlation_id,
        )

        self._log_inbound(correlation_id, task_id, manifest, residual_findings)
        return clean, result

    # ── Inbound task scan (no correlation id needed) ───────────────────────

    def scan_task(self, task, repo=None) -> GatewayResult:
        """
        Scan all user-controlled Task fields before execution begins.
        Mutates task.description and task.title in place if PII is found.
        Writes a privacy_scan audit entry via repo if provided.
        """
        if not _ENABLED:
            return GatewayResult(original_hash="", clean_content="")

        result = self.scan_and_redact(
            "\n".join([
                task.description or "",
                task.title or "",
                json.dumps(task.context or {}),
                json.dumps(task.acceptance_criteria or []),
            ]),
            location="task_input",
            task_id=task.id,
        )

        if result.has_findings and _REDACT:
            desc_r = self.scan_and_redact(task.description or "", "task_description", task.id)
            task.description = desc_r.clean_content
            title_r = self.scan_and_redact(task.title or "", "task_title", task.id)
            task.title = title_r.clean_content

        if repo and result.has_findings:
            try:
                repo.write_audit(task.id, "privacy_scan", "privacy_gateway",
                                 result.to_audit_dict())
            except Exception as exc:
                log.warning("Could not write privacy audit entry", error=str(exc))

        return result

    # ── One-shot scan (no ledger) ──────────────────────────────────────────

    def scan_and_redact(
        self,
        content:  str,
        location: str = "unknown",
        task_id:  str = "",
    ) -> GatewayResult:
        """
        Stateless scan — no correlation id, no ledger entry.
        Used for task_input scans and CLI tooling.
        """
        if not content or not content.strip():
            return GatewayResult(
                original_hash=_sha256(content or ""),
                clean_content=content or "",
            )
        if not _ENABLED:
            return GatewayResult(
                original_hash=_sha256(content),
                clean_content=content,
            )

        original_hash = _sha256(content)
        findings: List[PrivacyFinding] = []

        # Detection pass (all detectors)
        findings += self._pii.detect(content, location)
        findings += self._gdpr.detect(content, location)
        findings += self._hipaa.detect(content, location)
        findings += self._residency.detect(content, location)
        findings += self._leak.detect(content, location)

        # Redact pass (no manifest — use throw-away)
        clean = content
        n_redacted = 0
        if _REDACT and findings:
            throwaway = RedactionManifest(
                correlation_id="stateless-" + _sha256(content)[:8],
                task_id=task_id,
            )
            clean, _ = self._redact_text(clean, location, throwaway)
            n_redacted = throwaway.redaction_count()

        high = [f for f in findings if f.severity == "HIGH"]
        blocked = _GATE == "fail" and bool(high)

        result = GatewayResult(
            original_hash=original_hash,
            clean_content=clean,
            findings=findings,
            blocked=blocked,
            redaction_count=n_redacted,
        )

        if findings:
            fn = log.error if blocked else log.warning
            fn("Privacy gateway findings", location=location, task_id=task_id,
               summary=result.summary(), blocked=blocked)
            if _LOG_LEVEL == "full":
                for f in findings:
                    log.info("Privacy finding detail", **f.to_dict(), task_id=task_id)
        else:
            log.debug("Privacy gateway clean", location=location, task_id=task_id)

        return result

    # ── Internal helpers ───────────────────────────────────────────────────

    def _redact_text(
        self,
        text:     str,
        location: str,
        manifest: RedactionManifest,
    ) -> Tuple[str, List[PrivacyFinding]]:
        """Apply all redacting detectors to text, updating manifest in place."""
        all_findings: List[PrivacyFinding] = []

        text, f = self._pii.redact_with_manifest(text, location, manifest)
        all_findings += f

        text, f = self._hipaa.redact_with_manifest(text, location, manifest)
        all_findings += f

        text, f = self._leak.redact_with_manifest(text, location, manifest)
        all_findings += f

        # GDPR and Residency are detect-only (contextual keywords, not values)
        all_findings += self._gdpr.detect(text, location)
        all_findings += self._residency.detect(text, location)

        return text, all_findings

    def _log_outbound(
        self,
        correlation_id: str,
        task_id:        str,
        manifest:       RedactionManifest,
        findings:       List[PrivacyFinding],
    ) -> None:
        if not findings:
            log.debug("Privacy: prompt clean", correlation_id=correlation_id, task_id=task_id)
            return
        cats = {}
        for f in findings:
            cats[f.category] = cats.get(f.category, 0) + 1
        log.warning(
            "Privacy: PII redacted from prompt",
            correlation_id=correlation_id,
            task_id=task_id,
            tokens_issued=manifest.redaction_count(),
            categories=cats,
        )
        if _LOG_LEVEL == "full":
            for f in findings:
                log.info("Privacy outbound finding", correlation_id=correlation_id,
                         **f.to_dict())

    def _log_inbound(
        self,
        correlation_id: str,
        task_id:        str,
        manifest:       RedactionManifest,
        residual:       List[PrivacyFinding],
    ) -> None:
        ctx = dict(
            correlation_id=correlation_id,
            task_id=task_id,
            prompt_tokens=manifest.redaction_count(),
            residual_findings=len(residual),
        )
        if residual:
            log.warning("Privacy: residual PII found in LLM response", **ctx)
            if _LOG_LEVEL == "full":
                for f in residual:
                    log.info("Privacy residual finding", correlation_id=correlation_id,
                             **f.to_dict())
        else:
            log.debug("Privacy: response clean", **ctx)


# ── Error ──────────────────────────────────────────────────────────────────────

class PrivacyViolationError(RuntimeError):
    """Raised when FORGE_PRIVACY_GATE=fail and HIGH-severity PII is found."""
    def __init__(self, message: str, result: GatewayResult):
        super().__init__(f"Privacy gate blocked: {message}")
        self.result = result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# ── Module-level singleton ─────────────────────────────────────────────────────

_gateway: Optional[PrivacyGateway] = None
_gateway_lock = threading.Lock()


def get_privacy_gateway() -> PrivacyGateway:
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = PrivacyGateway()
    return _gateway
