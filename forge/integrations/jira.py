"""
FORGE Jira Integration
======================
Read and manage Jira issues via REST API v3.

Env vars
--------
  JIRA_URL        your Jira instance URL e.g. https://yourcompany.atlassian.net
  JIRA_EMAIL      account email for basic auth
  JIRA_API_TOKEN  API token from https://id.atlassian.com/manage-profile/security/api-tokens
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from forge.telemetry.logger import get_logger

log = get_logger("jira")

_JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
_EMAIL    = os.environ.get("JIRA_EMAIL", "")
_TOKEN    = os.environ.get("JIRA_API_TOKEN", "")
_TIMEOUT  = 30


class JiraError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Jira API {status}: {message}")


def _auth_header() -> str:
    if not _EMAIL or not _TOKEN:
        raise JiraError(0, "JIRA_EMAIL and JIRA_API_TOKEN must be set")
    creds = base64.b64encode(f"{_EMAIL}:{_TOKEN}".encode()).decode()
    return f"Basic {creds}"


def _request(method: str, path: str, body: Optional[dict] = None) -> Any:
    if not _JIRA_URL:
        raise JiraError(0, "JIRA_URL not set")
    url = f"{_JIRA_URL}/rest/api/3{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": _auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            errors = json.loads(body_text)
            msg = errors.get("errorMessages", [body_text])
            msg = msg[0] if msg else body_text
        except Exception:
            msg = body_text
        raise JiraError(e.code, msg)


# ── Issues ────────────────────────────────────────────────────────────────────

def get_issue(issue_key: str) -> dict:
    """Get a Jira issue by key e.g. PROJ-123."""
    return _request("GET", f"/issue/{issue_key}")


def search_issues(jql: str, max_results: int = 20) -> List[dict]:
    """Search issues using JQL. Returns list of issue dicts."""
    result = _request("POST", "/search", {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "assignee", "priority", "description",
                   "created", "updated", "labels", "components"],
    })
    return result.get("issues", [])


def create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    priority: str = "Medium",
    labels: Optional[List[str]] = None,
    assignee_id: Optional[str] = None,
) -> dict:
    """Create a new Jira issue."""
    fields: dict = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
        "priority": {"name": priority},
    }
    if description:
        # Jira v3 uses Atlassian Document Format
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": description}
            ]}],
        }
    if labels:
        fields["labels"] = labels
    if assignee_id:
        fields["assignee"] = {"id": assignee_id}

    result = _request("POST", "/issue", {"fields": fields})
    log.info("Jira issue created", key=result.get("key"))
    return result


def update_issue(issue_key: str, fields: dict) -> None:
    """Update fields of an existing issue."""
    _request("PUT", f"/issue/{issue_key}", {"fields": fields})
    log.info("Jira issue updated", key=issue_key)


def transition_issue(issue_key: str, transition_name: str) -> None:
    """Transition an issue to a new status by transition name."""
    transitions = _request("GET", f"/issue/{issue_key}/transitions")
    match = next(
        (t for t in transitions.get("transitions", [])
         if t["name"].lower() == transition_name.lower()),
        None
    )
    if not match:
        available = [t["name"] for t in transitions.get("transitions", [])]
        raise JiraError(400, f"Transition {transition_name!r} not found. Available: {available}")
    _request("POST", f"/issue/{issue_key}/transitions", {"transition": {"id": match["id"]}})
    log.info("Jira issue transitioned", key=issue_key, transition=transition_name)


def add_comment(issue_key: str, comment: str) -> dict:
    """Add a comment to an issue."""
    body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": comment}
            ]}],
        }
    }
    return _request("POST", f"/issue/{issue_key}/comments", body)


def get_issue_summary(issue_key: str) -> str:
    """Return a human-readable summary of a Jira issue."""
    try:
        issue = get_issue(issue_key)
        fields = issue.get("fields", {})
        status = fields.get("status", {}).get("name", "unknown")
        assignee = (fields.get("assignee") or {}).get("displayName", "unassigned")
        priority = (fields.get("priority") or {}).get("name", "none")
        return (
            f"Issue: {issue_key}\n"
            f"Summary: {fields.get('summary', '')}\n"
            f"Status: {status}  Priority: {priority}  Assignee: {assignee}\n"
            f"Updated: {fields.get('updated', '')}"
        )
    except JiraError as e:
        return f"Jira error: {e}"


def list_project_issues(project_key: str, status: str = "", limit: int = 20) -> str:
    """Return readable list of issues for a project."""
    jql = f"project={project_key}"
    if status:
        jql += f" AND status='{status}'"
    jql += " ORDER BY updated DESC"
    try:
        issues = search_issues(jql, max_results=limit)
        if not issues:
            return f"No issues found for project {project_key}"
        lines = [f"Issues for {project_key} ({len(issues)} found):"]
        for i in issues:
            f = i.get("fields", {})
            lines.append(
                f"  {i['key']}: [{f.get('status', {}).get('name', '?')}] {f.get('summary', '')}"
            )
        return "\n".join(lines)
    except JiraError as e:
        return f"Jira error: {e}"