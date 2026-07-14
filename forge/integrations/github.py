"""
FORGE GitHub Integration
========================
Read repos, create issues, open pull requests, manage files.
Uses GitHub REST API v3 — no SDK dependency.

Env vars
--------
  GITHUB_TOKEN        personal access token or GitHub App token (required)
  GITHUB_API_URL      override for GitHub Enterprise (default: https://api.github.com)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from forge.telemetry.logger import get_logger

log = get_logger("github")

_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")
_TIMEOUT = 30


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub API {status}: {message}")


def _request(
    method: str,
    path: str,
    body: Optional[dict] = None,
    token: str = "",
) -> Any:
    """Make an authenticated GitHub API request."""
    tok = token or _TOKEN
    if not tok:
        raise GitHubError(0, "GITHUB_TOKEN not set")

    url = f"{_API_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "Forge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body_text).get("message", body_text)
        except Exception:
            msg = body_text
        raise GitHubError(e.code, msg)


# ── Repository ────────────────────────────────────────────────────────────────

def get_repo(owner: str, repo: str) -> dict:
    """Get repository metadata."""
    return _request("GET", f"/repos/{owner}/{repo}")


def list_files(owner: str, repo: str, path: str = "", ref: str = "") -> List[dict]:
    """List files in a directory. Returns [{name, path, type, download_url}]."""
    url = f"/repos/{owner}/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"
    result = _request("GET", url)
    if isinstance(result, list):
        return result
    return [result]  # single file


def get_file_content(owner: str, repo: str, path: str, ref: str = "") -> str:
    """Get decoded file content."""
    import base64
    url = f"/repos/{owner}/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"
    result = _request("GET", url)
    content = result.get("content", "")
    encoding = result.get("encoding", "base64")
    if encoding == "base64":
        return base64.b64decode(content.replace("\n", "")).decode("utf-8", errors="replace")
    return content


def create_or_update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
    sha: Optional[str] = None,
) -> dict:
    """Create or update a file in the repo. sha required for updates."""
    import base64
    encoded = base64.b64encode(content.encode()).decode()
    body: dict = {"message": message, "content": encoded, "branch": branch}
    if sha:
        body["sha"] = sha
    return _request("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)


def search_code(owner: str, repo: str, query: str) -> List[dict]:
    """Search code in a repo. Returns [{name, path, html_url}]."""
    import urllib.parse
    q = urllib.parse.quote(f"{query} repo:{owner}/{repo}")
    result = _request("GET", f"/search/code?q={q}&per_page=20")
    return result.get("items", [])


# ── Issues ────────────────────────────────────────────────────────────────────

def list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: Optional[List[str]] = None,
    limit: int = 20,
) -> List[dict]:
    """List repository issues."""
    params = f"state={state}&per_page={limit}"
    if labels:
        params += "&labels=" + ",".join(labels)
    result = _request("GET", f"/repos/{owner}/{repo}/issues?{params}")
    return result if isinstance(result, list) else []


def get_issue(owner: str, repo: str, number: int) -> dict:
    """Get a specific issue."""
    return _request("GET", f"/repos/{owner}/{repo}/issues/{number}")


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
) -> dict:
    """Create a new issue. Returns created issue dict."""
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    result = _request("POST", f"/repos/{owner}/{repo}/issues", payload)
    log.info("Issue created", owner=owner, repo=repo, number=result.get("number"))
    return result


def add_issue_comment(owner: str, repo: str, number: int, body: str) -> dict:
    """Add a comment to an issue."""
    return _request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{number}/comments",
        {"body": body},
    )


def close_issue(owner: str, repo: str, number: int) -> dict:
    """Close an issue."""
    return _request(
        "PATCH",
        f"/repos/{owner}/{repo}/issues/{number}",
        {"state": "closed"},
    )


# ── Pull Requests ─────────────────────────────────────────────────────────────

def list_prs(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = 20,
) -> List[dict]:
    """List pull requests."""
    result = _request("GET", f"/repos/{owner}/{repo}/pulls?state={state}&per_page={limit}")
    return result if isinstance(result, list) else []


def get_pr(owner: str, repo: str, number: int) -> dict:
    """Get a specific pull request."""
    return _request("GET", f"/repos/{owner}/{repo}/pulls/{number}")


def create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    draft: bool = False,
) -> dict:
    """Create a pull request. head is the branch with changes."""
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }
    result = _request("POST", f"/repos/{owner}/{repo}/pulls", payload)
    log.info("PR created", owner=owner, repo=repo, number=result.get("number"))
    return result


def add_pr_review(
    owner: str,
    repo: str,
    number: int,
    body: str,
    event: str = "COMMENT",
) -> dict:
    """Add a review to a PR. event: APPROVE|REQUEST_CHANGES|COMMENT."""
    return _request(
        "POST",
        f"/repos/{owner}/{repo}/pulls/{number}/reviews",
        {"body": body, "event": event},
    )


# ── Branches ──────────────────────────────────────────────────────────────────

def list_branches(owner: str, repo: str) -> List[dict]:
    """List branches."""
    result = _request("GET", f"/repos/{owner}/{repo}/branches?per_page=50")
    return result if isinstance(result, list) else []


def create_branch(owner: str, repo: str, branch: str, from_branch: str = "main") -> dict:
    """Create a new branch from an existing one."""
    # Get SHA of base branch
    ref = _request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
    sha = ref["object"]["sha"]
    return _request(
        "POST",
        f"/repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": sha},
    )


# ── Commits ───────────────────────────────────────────────────────────────────

def list_commits(owner: str, repo: str, branch: str = "main", limit: int = 10) -> List[dict]:
    """List recent commits."""
    result = _request(
        "GET",
        f"/repos/{owner}/{repo}/commits?sha={branch}&per_page={limit}",
    )
    return result if isinstance(result, list) else []


# ── Convenience summary ───────────────────────────────────────────────────────

def repo_summary(owner: str, repo: str) -> str:
    """Return a human-readable summary of a repo for agent consumption."""
    try:
        r = get_repo(owner, repo)
        issues = list_issues(owner, repo, limit=5)
        prs    = list_prs(owner, repo, limit=5)
        branches = list_branches(owner, repo)
        branch_names = [b["name"] for b in branches[:10]]

        lines = [
            f"Repository: {owner}/{repo}",
            f"Description: {r.get('description', 'none')}",
            f"Language: {r.get('language', 'unknown')}",
            f"Stars: {r.get('stargazers_count', 0)}  Forks: {r.get('forks_count', 0)}",
            f"Default branch: {r.get('default_branch', 'main')}",
            f"Open issues: {r.get('open_issues_count', 0)}",
            f"Branches: {', '.join(branch_names)}",
            "",
            f"Recent open issues ({len(issues)}):",
        ]
        for i in issues:
            lines.append(f"  #{i['number']}: {i['title']}")
        lines.append(f"\nOpen PRs ({len(prs)}):")
        for p in prs:
            lines.append(f"  #{p['number']}: {p['title']} ({p['head']['ref']} → {p['base']['ref']})")
        return "\n".join(lines)
    except GitHubError as e:
        return f"GitHub error: {e}"