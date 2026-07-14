"""
FORGE Tutor — Evidence Layer
============================
Normalises all input signals into Evidence objects.

Sources handled:
  • Execution traces   (JSON log lines from forge.telemetry.logger)
  • Repository scans   (RepositoryReport from forge.intelligence.repository)
  • Test results       (TestIntelligenceReport from forge.intelligence.analyser)
  • Task outcomes      (TaskResult + ExecutionContext from forge.orchestration)
  • External repos     (git clone → RepositoryReport)
  • Specifications     (markdown files: requirements.md, design.md, ADRs)

All evidence is stateless — an Evidence object is just a labelled,
timestamped payload. The PrincipleEngine decides what it means.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Evidence types ─────────────────────────────────────────────────────────────

EVIDENCE_TYPES = {
    "trace":        "Execution trace (JSON log lines)",
    "repo_scan":    "Repository structural analysis",
    "test_result":  "Test Intelligence report",
    "task_outcome": "Task result summary",
    "spec":         "Specification or ADR document",
    "external_repo":"External reference repository analysis",
}


@dataclass
class Evidence:
    """
    One normalised unit of information from any source.

    source_type:  one of EVIDENCE_TYPES keys
    source_id:    task_id, repo_name, file path, etc.
    payload:      structured data specific to source_type
    timestamp:    when this evidence was collected
    quality:      0.0–1.0 — how reliable/complete the source is
    tags:         used for filtering and clustering
    """
    source_type:  str
    source_id:    str
    payload:      Dict[str, Any]
    timestamp:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    quality:      float = 1.0
    tags:         List[str] = field(default_factory=list)


# ── Trace evidence ─────────────────────────────────────────────────────────────

@dataclass
class TraceEvent:
    """One parsed log line from a Forge execution trace."""
    timestamp:      str
    task_id:        str
    agent:          str
    event:          str
    severity:       str
    logger:         str
    duration_ms:    Optional[int]   = None
    provider:       Optional[str]   = None
    model:          Optional[str]   = None
    tokens:         Optional[int]   = None
    error:          Optional[str]   = None
    extra:          Dict[str, Any]  = field(default_factory=dict)


def parse_trace(raw: str) -> List[TraceEvent]:
    """
    Parse a multi-line JSON execution trace into TraceEvent objects.
    Non-JSON lines are silently skipped.
    """
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
            events.append(TraceEvent(
                timestamp   = d.get("timestamp", ""),
                task_id     = d.get("task_id", ""),
                agent       = d.get("agent", "system"),
                event       = d.get("event", ""),
                severity    = d.get("severity", "INFO"),
                logger      = d.get("logger", ""),
                duration_ms = d.get("duration_ms"),
                provider    = d.get("provider"),
                model       = d.get("model"),
                tokens      = d.get("tokens"),
                error       = d.get("error"),
                extra       = {
                    k: v for k, v in d.items()
                    if k not in {"timestamp","task_id","agent","event","severity",
                                 "logger","duration_ms","provider","model","tokens","error",
                                 "correlation_id"}
                },
            ))
        except (json.JSONDecodeError, KeyError):
            continue
    return events


def evidence_from_trace(trace_text: str, task_id: str = "") -> Evidence:
    """Convert a raw execution trace string into a single Evidence object."""
    events   = parse_trace(trace_text)
    phases   = sorted({e.agent for e in events})
    errors   = [e for e in events if e.severity in ("ERROR", "CRITICAL")]
    warnings = [e for e in events if e.severity == "WARNING"]
    outcome  = "COMPLETE" if not errors else "FAILED"
    durations = {
        e.agent: e.duration_ms
        for e in events if e.duration_ms and e.duration_ms > 0
    }
    providers = list({e.provider for e in events if e.provider})

    return Evidence(
        source_type = "trace",
        source_id   = task_id or (events[0].task_id if events else "unknown"),
        quality     = 0.9 if events else 0.3,
        tags        = ["trace", outcome.lower()] + providers,
        payload     = {
            "event_count":     len(events),
            "agents_seen":     phases,
            "outcome":         outcome,
            "error_events":    [{"agent": e.agent, "event": e.event, "error": e.error}
                                 for e in errors[:10]],
            "warning_events":  [{"agent": e.agent, "event": e.event}
                                 for e in warnings[:10]],
            "phase_durations": durations,
            "providers_used":  providers,
            "deterministic_rejections": [
                e.event for e in events if "deterministic" in e.event.lower()
            ],
            "recovery_events": [
                e.event for e in events if "repair" in e.event.lower()
                                         or "replan" in e.event.lower()
            ],
        },
    )


# ── Specification evidence ─────────────────────────────────────────────────────

def evidence_from_spec(path: str) -> Evidence:
    """
    Parse a markdown specification file (requirements.md, design.md,
    CLAUDE.md, ADR-*.md, tasks.md) into Evidence.

    Extracts:
      - section headings (structural decomposition)
      - explicit rules/constraints (lines starting with -, *, must, shall, never)
      - file references (anything ending in .py, .md, .yaml)
      - decision markers (## Decision, ## Status, ## Context)
    """
    p = Path(path)
    if not p.exists():
        return Evidence(
            source_type = "spec",
            source_id   = path,
            quality     = 0.0,
            payload     = {"error": "file not found"},
        )

    text   = p.read_text(encoding="utf-8", errors="replace")
    lines  = text.splitlines()

    headings    = [l.lstrip("#").strip() for l in lines if l.startswith("#")]
    rules       = [
        l.strip() for l in lines
        if re.match(r"^[\-\*]\s", l)
        or any(l.lower().lstrip().startswith(kw) for kw in
               ("must", "shall", "never", "always", "do not", "required"))
    ]
    file_refs   = re.findall(r"\b[\w/\-]+\.(?:py|md|yaml|yml|json|toml)\b", text)
    decisions   = re.findall(r"(?:##\s*(?:Decision|Status|Context).*?)(?=\n##|\Z)",
                             text, re.DOTALL | re.IGNORECASE)

    return Evidence(
        source_type = "spec",
        source_id   = path,
        quality     = min(1.0, len(rules) / 10 + 0.3),
        tags        = ["spec", p.stem.lower().replace("-", "_")],
        payload     = {
            "title":       p.stem,
            "headings":    headings[:20],
            "rules":       rules[:30],
            "file_refs":   list(set(file_refs))[:15],
            "decisions":   [d.strip()[:200] for d in decisions[:5]],
            "word_count":  len(text.split()),
        },
    )


# ── Repository evidence ────────────────────────────────────────────────────────

def evidence_from_repo_report(report) -> Evidence:
    """Convert a RepositoryReport into Evidence."""
    return Evidence(
        source_type = "repo_scan",
        source_id   = report.repo_name,
        quality     = min(1.0, report.total_files / 20),
        tags        = ["repo_scan"] + report.domain_tags + report.frameworks[:3],
        payload     = {
            "repo_name":       report.repo_name,
            "packages":        report.packages,
            "frameworks":      report.frameworks,
            "entry_points":    report.entry_points[:5],
            "patterns":        [{"pattern": p.pattern, "confidence": p.confidence,
                                  "evidence": p.evidence}
                                 for p in report.patterns[:8]],
            "layer_map":       {k: v[:4] for k, v in report.layer_map.items()},
            "domain_tags":     report.domain_tags,
            "as_is_prompt":    report.to_as_is_prompt(max_chars=3000),
            "stats": {
                "files":      report.total_files,
                "lines":      report.total_lines,
                "classes":    report.total_classes,
                "functions":  report.total_functions,
            },
        },
    )


# ── Test result evidence ───────────────────────────────────────────────────────

def evidence_from_test_report(report, task_id: str = "") -> Evidence:
    """Convert a TestIntelligenceReport into Evidence."""
    return Evidence(
        source_type = "test_result",
        source_id   = task_id or report.source_hash,
        quality     = 0.8,
        tags        = ["test_result", "intelligence"],
        payload     = {
            "critical_paths":  [{"entry": cp.entry_point, "score": cp.risk_score,
                                  "reason": cp.reason}
                                 for cp in report.critical_paths[:5]],
            "risk_areas":      [{"category": ra.category, "location": ra.location,
                                  "score": ra.risk_score}
                                 for ra in report.risk_areas[:8]],
            "dead_code_count": len(report.dead_code),
            "untested_count":  len(report.untested_areas),
            "complexity_mean": report.complexity_mean,
            "complexity_max":  report.complexity_max,
        },
    )


# ── Task outcome evidence ──────────────────────────────────────────────────────

def evidence_from_task_result(task, result, ctx=None) -> Evidence:
    """Convert a completed Task + TaskResult into Evidence."""
    outcome = "COMPLETE" if result.success else "FAILED"
    return Evidence(
        source_type = "task_outcome",
        source_id   = task.id,
        quality     = 0.95,
        tags        = ["task_outcome", outcome.lower(), task.task_type.value],
        payload     = {
            "task_id":         task.id,
            "task_type":       task.task_type.value,
            "outcome":         outcome,
            "success":         result.success,
            "error":           result.error or "",
            "tokens_used":     result.tokens_used or 0,
            "provider":        result.provider_used or "",
            "validation":      result.validation_passed,
            "review_passed":   result.review_passed,
            "tests_passed":    result.tests_passed,
            "repair_attempts": task.context.get("repair_attempt", 0),
            "ti_summary":      task.context.get("test_intelligence", {}),
            "repo_as_is":      bool(task.context.get("repository_as_is")),
        },
    )
