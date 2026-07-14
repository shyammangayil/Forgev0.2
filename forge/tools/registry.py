"""
FORGE Tool Registry
===================
Defines every tool that agents can invoke, and the executor that runs them.

Tools are plain Python functions. Each is described by a ToolSpec that
carries the JSON schema the LLM sees when deciding which tool to call.

Adding a new tool:
  1. Write a Python function tool_<name>(args: dict) -> ToolResult
  2. Register it in TOOL_REGISTRY below
  3. That's it — agents pick it up automatically

Safety model
------------
- Bash execution is disabled by default (FORGE_ALLOW_BASH=true to enable)
- All filesystem writes are confined to the configured workspace directory
- HTTP fetch is allowed to any URL (no allowlist — operators can add one)
- Git clone depth is always 1 (shallow) to avoid huge downloads

Env vars
--------
  FORGE_ALLOW_BASH=true     enable bash execution (default: false)
  FORGE_WORKSPACE=workspace  root dir for all file writes (default: workspace)
  FORGE_TOOL_TIMEOUT=30      seconds per tool call (default: 30)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forge.telemetry.logger import get_logger

log = get_logger("tools")

_ALLOW_BASH   = os.environ.get("FORGE_ALLOW_BASH", "false").lower() == "true"
_TOOL_TIMEOUT = int(os.environ.get("FORGE_TOOL_TIMEOUT", "30"))
# NOTE: _WORKSPACE is intentionally NOT a module-level constant.
# Always call _workspace() so runtime config changes (--output flag,
# FORGE_WORKSPACE env var, per-task override) are always honoured.


def _workspace() -> Path:
    """Return the current workspace root from live config."""
    try:
        from forge.config import get_config
        return Path(get_config().workspace_dir)
    except Exception:
        return Path(os.environ.get("FORGE_WORKSPACE", "workspace")).resolve()


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    success: bool
    output: str                    # always a string — shown back to the LLM
    error: Optional[str] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]     # JSON Schema object for the "parameters" field
    fn: Callable[[Dict], ToolResult]

    def to_openai_schema(self) -> dict:
        """Return the tool definition in OpenAI / Ollama function-call format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict:
        """Return the tool definition in Anthropic tool-use format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_read_file(args: dict) -> ToolResult:
    """Read a file from the workspace or an absolute path."""
    path_str = args.get("path", "")
    if not path_str:
        return ToolResult(success=False, output="", error="path is required")

    path = Path(path_str)
    # If relative, resolve inside workspace
    if not path.is_absolute():
        path = _workspace() / path

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        # Cap output to avoid flooding context
        if len(content) > 8000:
            content = content[:8000] + f"\n\n... [truncated — {len(content) - 8000} more chars]"
        return ToolResult(success=True, output=content)
    except FileNotFoundError:
        return ToolResult(success=False, output="", error=f"File not found: {path}")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_write_file(args: dict) -> ToolResult:
    """Write content to a file inside the workspace."""
    path_str = args.get("path", "")
    content  = args.get("content", "")
    if not path_str:
        return ToolResult(success=False, output="", error="path is required")

    path = Path(path_str)
    if not path.is_absolute():
        path = _workspace() / path

    # Confine writes to workspace
    try:
        path.resolve().relative_to(_workspace().resolve())
    except ValueError:
        return ToolResult(success=False, output="",
                          error=f"Write outside workspace is not allowed: {path}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(success=True, output=f"Written {len(content)} chars to {path}")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_list_files(args: dict) -> ToolResult:
    """List files in a directory (relative to workspace or absolute)."""
    path_str = args.get("path", ".")
    path = Path(path_str)
    if not path.is_absolute():
        path = _workspace() / path

    try:
        entries = sorted(path.iterdir())
        lines = []
        for e in entries:
            kind = "dir" if e.is_dir() else "file"
            size = e.stat().st_size if e.is_file() else 0
            lines.append(f"{kind:4s}  {size:>8,}  {e.name}")
        output = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult(success=True, output=output)
    except FileNotFoundError:
        return ToolResult(success=False, output="", error=f"Directory not found: {path}")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_git_clone(args: dict) -> ToolResult:
    """Shallow-clone a git repository into the workspace."""
    url    = args.get("url", "")
    dest   = args.get("dest", "")
    if not url:
        return ToolResult(success=False, output="", error="url is required")
    if not shutil.which("git"):
        return ToolResult(success=False, output="", error="git not found in PATH")

    dest_path = _workspace() / (dest or Path(url).stem)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest_path)],
            capture_output=True, text=True,
            timeout=_TOOL_TIMEOUT * 4,   # clone can be slow
        )
        if result.returncode != 0:
            return ToolResult(success=False, output="",
                              error=result.stderr.strip() or "git clone failed")
        # Return a file tree summary
        files = [str(p.relative_to(dest_path))
                 for p in dest_path.rglob("*") if p.is_file()][:60]
        summary = f"Cloned to {dest_path}\n\nFiles ({len(files)} shown):\n" + "\n".join(files)
        return ToolResult(success=True, output=summary)
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output="", error="git clone timed out")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_http_fetch(args: dict) -> ToolResult:
    """Fetch the content of a URL (text only)."""
    url     = args.get("url", "")
    headers = args.get("headers", {})
    if not url:
        return ToolResult(success=False, output="", error="url is required")

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TOOL_TIMEOUT) as resp:
            raw = resp.read(65536)   # max 64 KB
            text = raw.decode("utf-8", errors="replace")
            if len(text) > 8000:
                text = text[:8000] + f"\n\n... [truncated]"
            return ToolResult(success=True, output=text)
    except urllib.error.HTTPError as e:
        return ToolResult(success=False, output="", error=f"HTTP {e.code}: {e.reason}")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_web_search(args: dict) -> ToolResult:
    """
    Search the web using DuckDuckGo Instant Answer API (no key required).
    For richer results, set FORGE_SEARCH_PROVIDER=tavily and TAVILY_API_KEY.
    """
    import urllib.parse

    query    = args.get("query", "")
    max_results = min(int(args.get("max_results", 5)), 10)
    if not query:
        return ToolResult(success=False, output="", error="query is required")

    provider = os.environ.get("FORGE_SEARCH_PROVIDER", "duckduckgo").lower()

    # ── Tavily (preferred when key available) ─────────────────────────────
    if provider == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            return ToolResult(success=False, output="",
                              error="TAVILY_API_KEY not set")
        payload = json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TOOL_TIMEOUT) as resp:
                data = json.loads(resp.read())
            lines = []
            if data.get("answer"):
                lines.append(f"Summary: {data['answer']}\n")
            for r in data.get("results", [])[:max_results]:
                lines.append(f"[{r.get('title', '')}]\n{r.get('url', '')}\n{r.get('content', '')[:300]}\n")
            return ToolResult(success=True, output="\n---\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc))

    # ── DuckDuckGo (no key, rate-limited but free) ─────────────────────────
    encoded = urllib.parse.quote_plus(query)
    # DDG Instant Answer API — returns JSON with AbstractText + RelatedTopics
    url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1&no_html=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Forge/1.0"})
        with urllib.request.urlopen(req, timeout=_TOOL_TIMEOUT) as resp:
            data = json.loads(resp.read())

        lines = []
        abstract = data.get("AbstractText", "")
        if abstract:
            lines.append(f"Summary: {abstract}")
            lines.append(f"Source: {data.get('AbstractURL', '')}")

        for topic in data.get("RelatedTopics", [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                lines.append(f"\n- {topic['Text'][:200]}")

        if not lines:
            # Fall back to HTML scrape of DDG search
            html_url = f"https://html.duckduckgo.com/html/?q={encoded}"
            req2 = urllib.request.Request(html_url, headers={"User-Agent": "Forge/1.0"})
            with urllib.request.urlopen(req2, timeout=_TOOL_TIMEOUT) as resp2:
                html = resp2.read().decode("utf-8", errors="replace")
            # Extract result snippets with simple regex
            snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL
            )
            snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets[:max_results]]
            lines = snippets or ["No results found."]

        return ToolResult(success=True, output="\n".join(lines)[:6000])
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_bash(args: dict) -> ToolResult:
    """Execute a bash command (must be explicitly enabled via FORGE_ALLOW_BASH=true)."""
    if not _ALLOW_BASH:
        return ToolResult(
            success=False, output="",
            error="Bash execution is disabled. Set FORGE_ALLOW_BASH=true to enable.",
        )
    command = args.get("command", "")
    if not command:
        return ToolResult(success=False, output="", error="command is required")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=_TOOL_TIMEOUT, cwd=str(_workspace()),
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 8000:
            output = output[:8000] + "\n... [truncated]"
        success = result.returncode == 0
        return ToolResult(
            success=success,
            output=output or "(no output)",
            error=None if success else f"exit code {result.returncode}",
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output="", error=f"Command timed out after {_TOOL_TIMEOUT}s")
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


def tool_search_files(args: dict) -> ToolResult:
    """Search for a pattern in files under the workspace."""
    pattern = args.get("pattern", "")
    path    = args.get("path", ".")
    if not pattern:
        return ToolResult(success=False, output="", error="pattern is required")

    root = Path(path) if Path(path).is_absolute() else _workspace() / path
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return ToolResult(success=False, output="", error=f"Invalid regex: {e}")

    matches = []
    for fpath in root.rglob("*"):
        if not fpath.is_file():
            continue
        try:
            for i, line in enumerate(fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = fpath.relative_to(root)
                    matches.append(f"{rel}:{i}: {line.strip()}")
                    if len(matches) >= 50:
                        break
        except Exception:
            continue
        if len(matches) >= 50:
            break

    if not matches:
        return ToolResult(success=True, output="No matches found.")
    return ToolResult(success=True, output="\n".join(matches))


# ── Registry ──────────────────────────────────────────────────────────────────

def tool_remember(args: dict) -> ToolResult:
    """Store a fact or finding in cross-task memory."""
    from forge.memory.store import get_memory_store
    memory_type = args.get("type", "fact")
    key         = args.get("key", "")
    value       = args.get("value", "")
    tags        = args.get("tags", [])
    if not key or not value:
        return ToolResult(success=False, output="", error="key and value are required")
    try:
        store = get_memory_store()
        entry = store.remember(memory_type, key, value, tags=tags)
        return ToolResult(success=True, output=f"Remembered: [{memory_type}] {key}")
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_recall(args: dict) -> ToolResult:
    """Retrieve relevant memories by query."""
    from forge.memory.store import get_memory_store
    query       = args.get("query", "")
    memory_type = args.get("type", None)
    if not query:
        return ToolResult(success=False, output="", error="query is required")
    try:
        store = get_memory_store()
        entries = store.recall(query, memory_type=memory_type)
        if not entries:
            return ToolResult(success=True, output="No relevant memories found.")
        return ToolResult(success=True, output=store.format_for_prompt(entries))
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_forget(args: dict) -> ToolResult:
    """Archive a memory by key."""
    from forge.memory.store import get_memory_store
    key = args.get("key", "")
    if not key:
        return ToolResult(success=False, output="", error="key is required")
    try:
        store = get_memory_store()
        deleted = store.forget(key)
        return ToolResult(success=True, output=f"Forgot: {key}" if deleted else f"Not found: {key}")
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_github_read(args: dict) -> ToolResult:
    """Read from GitHub: repo info, files, issues, or PRs."""
    from forge.integrations.github import (
        repo_summary, get_file_content, list_issues, list_prs,
        search_code, list_files, GitHubError
    )
    action = args.get("action", "summary")
    owner  = args.get("owner", "")
    repo   = args.get("repo", "")
    if not owner or not repo:
        return ToolResult(success=False, output="", error="owner and repo are required")
    try:
        if action == "summary":
            return ToolResult(success=True, output=repo_summary(owner, repo))
        elif action == "file":
            path = args.get("path", "")
            content = get_file_content(owner, repo, path, args.get("ref", ""))
            if len(content) > 8000:
                content = content[:8000] + "\n... [truncated]"
            return ToolResult(success=True, output=content)
        elif action == "files":
            files = list_files(owner, repo, args.get("path", ""), args.get("ref", ""))
            lines = [f"{'DIR ' if f.get('type')=='dir' else 'FILE'} {f['path']}" for f in files[:50]]
            return ToolResult(success=True, output="\n".join(lines))
        elif action == "issues":
            issues = list_issues(owner, repo, args.get("state", "open"), limit=20)
            lines = [f"#{i['number']}: [{i['state']}] {i['title']}" for i in issues]
            return ToolResult(success=True, output="\n".join(lines) or "No issues found")
        elif action == "prs":
            prs = list_prs(owner, repo, args.get("state", "open"), limit=20)
            lines = [f"#{p['number']}: {p['title']} ({p['head']['ref']}→{p['base']['ref']})" for p in prs]
            return ToolResult(success=True, output="\n".join(lines) or "No PRs found")
        elif action == "search":
            results = search_code(owner, repo, args.get("query", ""))
            lines = [f"{r['path']}: {r.get('html_url', '')}" for r in results[:20]]
            return ToolResult(success=True, output="\n".join(lines) or "No results")
        else:
            return ToolResult(success=False, output="", error=f"Unknown action: {action}")
    except GitHubError as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_github_write(args: dict) -> ToolResult:
    """Write to GitHub: create issues, PRs, or file commits."""
    from forge.integrations.github import (
        create_issue, create_pr, create_branch,
        create_or_update_file, add_issue_comment, GitHubError
    )
    action = args.get("action", "")
    owner  = args.get("owner", "")
    repo   = args.get("repo", "")
    if not owner or not repo:
        return ToolResult(success=False, output="", error="owner and repo are required")
    try:
        if action == "create_issue":
            result = create_issue(owner, repo,
                title=args.get("title", ""),
                body=args.get("body", ""),
                labels=args.get("labels", []),
            )
            return ToolResult(success=True,
                output=f"Issue created: #{result['number']} {result['html_url']}")
        elif action == "create_pr":
            result = create_pr(owner, repo,
                title=args.get("title", ""),
                body=args.get("body", ""),
                head=args.get("head", ""),
                base=args.get("base", "main"),
            )
            return ToolResult(success=True,
                output=f"PR created: #{result['number']} {result['html_url']}")
        elif action == "create_branch":
            create_branch(owner, repo,
                branch=args.get("branch", ""),
                from_branch=args.get("from", "main"),
            )
            return ToolResult(success=True, output=f"Branch created: {args.get('branch')}")
        elif action == "commit_file":
            result = create_or_update_file(owner, repo,
                path=args.get("path", ""),
                content=args.get("content", ""),
                message=args.get("message", "forge: update file"),
                branch=args.get("branch", "main"),
                sha=args.get("sha"),
            )
            return ToolResult(success=True, output=f"File committed: {args.get('path')}")
        elif action == "comment_issue":
            add_issue_comment(owner, repo,
                number=int(args.get("number", 0)),
                body=args.get("body", ""),
            )
            return ToolResult(success=True, output="Comment added")
        else:
            return ToolResult(success=False, output="", error=f"Unknown action: {action}")
    except GitHubError as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_jira(args: dict) -> ToolResult:
    """Interact with Jira: read issues, create tickets, add comments."""
    from forge.integrations.jira import (
        get_issue_summary, list_project_issues, create_issue,
        add_comment, transition_issue, JiraError
    )
    action = args.get("action", "get")
    try:
        if action == "get":
            return ToolResult(success=True,
                output=get_issue_summary(args.get("issue_key", "")))
        elif action == "list":
            return ToolResult(success=True,
                output=list_project_issues(
                    args.get("project", ""),
                    status=args.get("status", ""),
                    limit=int(args.get("limit", 20)),
                ))
        elif action == "create":
            result = create_issue(
                project_key=args.get("project", ""),
                summary=args.get("summary", ""),
                description=args.get("description", ""),
                issue_type=args.get("issue_type", "Task"),
                priority=args.get("priority", "Medium"),
                labels=args.get("labels", []),
            )
            return ToolResult(success=True,
                output=f"Issue created: {result.get('key')} {result.get('self','')}")
        elif action == "comment":
            add_comment(args.get("issue_key", ""), args.get("comment", ""))
            return ToolResult(success=True, output="Comment added")
        elif action == "transition":
            transition_issue(args.get("issue_key", ""), args.get("transition", ""))
            return ToolResult(success=True,
                output=f"Transitioned {args.get('issue_key')} to {args.get('transition')}")
        else:
            return ToolResult(success=False, output="", error=f"Unknown action: {action}")
    except JiraError as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_slack(args: dict) -> ToolResult:
    """Send Slack messages or read channel history."""
    from forge.integrations.slack import (
        send_message, send_rich_message, get_channel_history,
        send_dm, SlackError
    )
    action = args.get("action", "send")
    try:
        if action == "send":
            result = send_message(
                text=args.get("text", ""),
                channel=args.get("channel", ""),
            )
            return ToolResult(success=True, output=f"Message sent (ts: {result.get('ts')})")
        elif action == "rich":
            send_rich_message(
                channel=args.get("channel", ""),
                title=args.get("title", ""),
                text=args.get("text", ""),
                fields=args.get("fields", []),
            )
            return ToolResult(success=True, output="Rich message sent")
        elif action == "history":
            messages = get_channel_history(
                args.get("channel", ""),
                limit=int(args.get("limit", 10)),
            )
            lines = [f"[{m.get('ts','')}] {m.get('text','')[:200]}" for m in messages]
            return ToolResult(success=True, output="\n".join(lines) or "No messages")
        elif action == "dm":
            send_dm(args.get("user_id", ""), args.get("text", ""))
            return ToolResult(success=True, output="DM sent")
        else:
            return ToolResult(success=False, output="", error=f"Unknown action: {action}")
    except SlackError as e:
        return ToolResult(success=False, output="", error=str(e))


def tool_generate_file(args: dict) -> ToolResult:
    """Generate a file: markdown, CSV, XLSX, DOCX, or PDF."""
    from forge.integrations.files import (
        write_markdown, write_csv, write_xlsx, write_docx, write_pdf
    )
    fmt      = args.get("format", "markdown").lower()
    filename = args.get("filename", "")
    content  = args.get("content", "")
    title    = args.get("title", "")
    subdir   = args.get("subdir", "")

    if not filename:
        return ToolResult(success=False, output="", error="filename is required")

    try:
        if fmt in ("md", "markdown", "txt", "text"):
            result = write_markdown(content, filename, subdir)
        elif fmt == "csv":
            rows = args.get("rows", [])
            if isinstance(rows, str):
                import json as _json
                rows = _json.loads(rows)
            result = write_csv(rows, filename, subdir)
        elif fmt in ("xlsx", "excel"):
            sheets = args.get("sheets", {})
            if isinstance(sheets, str):
                import json as _json
                sheets = _json.loads(sheets)
            result = write_xlsx(sheets, filename, subdir)
        elif fmt in ("docx", "word"):
            result = write_docx(content, filename, title=title, subdir=subdir)
        elif fmt == "pdf":
            result = write_pdf(content, filename, title=title, subdir=subdir)
        else:
            return ToolResult(success=False, output="",
                              error=f"Unknown format: {fmt}. Use: markdown, csv, xlsx, docx, pdf")

        if result.success:
            msg = str(result)
            if result.error:  # partial success (e.g. PDF fallback)
                msg += f"\nNote: {result.error}"
            return ToolResult(success=True, output=msg)
        else:
            return ToolResult(success=False, output="", error=result.error)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


TOOL_REGISTRY: Dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file",
        description="Read the contents of a file. Path is relative to the workspace unless absolute.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
            },
            "required": ["path"],
        },
        fn=tool_read_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="Write content to a file in the workspace. Creates parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path (relative to workspace)"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        fn=tool_write_file,
    ),
    "list_files": ToolSpec(
        name="list_files",
        description="List files and directories at a path. Default is workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list", "default": "."},
            },
            "required": [],
        },
        fn=tool_list_files,
    ),
    "git_clone": ToolSpec(
        name="git_clone",
        description="Shallow-clone a git repository into the workspace. Returns a file tree summary.",
        parameters={
            "type": "object",
            "properties": {
                "url":  {"type": "string", "description": "Repository URL to clone"},
                "dest": {"type": "string", "description": "Destination folder name inside workspace"},
            },
            "required": ["url"],
        },
        fn=tool_git_clone,
    ),
    "http_fetch": ToolSpec(
        name="http_fetch",
        description="Fetch the text content of a URL. Returns up to 8000 characters.",
        parameters={
            "type": "object",
            "properties": {
                "url":     {"type": "string", "description": "URL to fetch"},
                "headers": {"type": "object", "description": "Optional HTTP headers"},
            },
            "required": ["url"],
        },
        fn=tool_http_fetch,
    ),
    "bash": ToolSpec(
        name="bash",
        description=(
            "Execute a bash shell command in the workspace directory. "
            "Only available if FORGE_ALLOW_BASH=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
        fn=tool_bash,
    ),
    "search_files": ToolSpec(
        name="search_files",
        description="Search for a regex pattern across files in the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path":    {"type": "string", "description": "Directory to search (default: workspace root)"},
            },
            "required": ["pattern"],
        },
        fn=tool_search_files,
    ),
    "web_search": ToolSpec(
        name="web_search",
        description=(
            "Search the web for current information. Returns summaries and snippets. "
            "Use for: current events, documentation lookup, research, fact checking. "
            "Set FORGE_SEARCH_PROVIDER=tavily and TAVILY_API_KEY for richer results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (1-10)", "default": 5},
            },
            "required": ["query"],
        },
        fn=tool_web_search,
    ),
    "remember": ToolSpec(
        name="remember",
        description="Store a fact, preference, or finding in persistent cross-task memory.",
        parameters={
            "type": "object",
            "properties": {
                "type":  {"type": "string", "description": "Memory type: fact|preference|finding|error|context"},
                "key":   {"type": "string", "description": "Unique key for this memory"},
                "value": {"type": "string", "description": "The content to remember"},
                "tags":  {"type": "array",  "items": {"type": "string"}, "description": "Optional tags"},
            },
            "required": ["key", "value"],
        },
        fn=tool_remember,
    ),
    "recall": ToolSpec(
        name="recall",
        description="Retrieve relevant memories from previous tasks by keyword search.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in memory"},
                "type":  {"type": "string", "description": "Filter by memory type (optional)"},
            },
            "required": ["query"],
        },
        fn=tool_recall,
    ),
    "forget": ToolSpec(
        name="forget",
        description="Archive (soft-delete) a memory by its key.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key of the memory to forget"},
            },
            "required": ["key"],
        },
        fn=tool_forget,
    ),
    "github_read": ToolSpec(
        name="github_read",
        description=(
            "Read from GitHub. Actions: summary, file, files, issues, prs, search. "
            "Requires GITHUB_TOKEN env var."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "summary|file|files|issues|prs|search"},
                "owner":  {"type": "string", "description": "GitHub owner/org"},
                "repo":   {"type": "string", "description": "Repository name"},
                "path":   {"type": "string", "description": "File or directory path (for file/files actions)"},
                "query":  {"type": "string", "description": "Search query (for search action)"},
                "state":  {"type": "string", "description": "open|closed|all (for issues/prs)"},
                "ref":    {"type": "string", "description": "Branch or commit SHA"},
            },
            "required": ["owner", "repo"],
        },
        fn=tool_github_read,
    ),
    "github_write": ToolSpec(
        name="github_write",
        description=(
            "Write to GitHub. Actions: create_issue, create_pr, create_branch, "
            "commit_file, comment_issue. Requires GITHUB_TOKEN."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action":  {"type": "string", "description": "create_issue|create_pr|create_branch|commit_file|comment_issue"},
                "owner":   {"type": "string", "description": "GitHub owner/org"},
                "repo":    {"type": "string", "description": "Repository name"},
                "title":   {"type": "string", "description": "Issue or PR title"},
                "body":    {"type": "string", "description": "Issue or PR body"},
                "head":    {"type": "string", "description": "Source branch (for PRs)"},
                "base":    {"type": "string", "description": "Target branch (default: main)"},
                "branch":  {"type": "string", "description": "Branch name"},
                "path":    {"type": "string", "description": "File path (for commit_file)"},
                "content": {"type": "string", "description": "File content (for commit_file)"},
                "message": {"type": "string", "description": "Commit message"},
                "number":  {"type": "integer","description": "Issue number (for comment_issue)"},
                "labels":  {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action", "owner", "repo"],
        },
        fn=tool_github_write,
    ),
    "jira": ToolSpec(
        name="jira",
        description=(
            "Interact with Jira. Actions: get, list, create, comment, transition. "
            "Requires JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action":      {"type": "string", "description": "get|list|create|comment|transition"},
                "issue_key":   {"type": "string", "description": "Issue key e.g. PROJ-123"},
                "project":     {"type": "string", "description": "Project key e.g. PROJ"},
                "summary":     {"type": "string", "description": "Issue summary (for create)"},
                "description": {"type": "string", "description": "Issue description (for create)"},
                "issue_type":  {"type": "string", "description": "Task|Bug|Story|Epic"},
                "priority":    {"type": "string", "description": "Highest|High|Medium|Low"},
                "comment":     {"type": "string", "description": "Comment text"},
                "transition":  {"type": "string", "description": "Transition name e.g. In Progress"},
                "status":      {"type": "string", "description": "Filter by status (for list)"},
                "labels":      {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        },
        fn=tool_jira,
    ),
    "slack": ToolSpec(
        name="slack",
        description=(
            "Send Slack messages or read channel history. "
            "Actions: send, rich, history, dm. Requires SLACK_BOT_TOKEN."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action":  {"type": "string", "description": "send|rich|history|dm"},
                "channel": {"type": "string", "description": "Channel ID or name"},
                "text":    {"type": "string", "description": "Message text"},
                "title":   {"type": "string", "description": "Title (for rich messages)"},
                "fields":  {"type": "array",  "description": "Key-value fields [{title, value}]"},
                "user_id": {"type": "string", "description": "User ID for DM"},
                "limit":   {"type": "integer","description": "Message limit for history"},
            },
            "required": ["action"],
        },
        fn=tool_slack,
    ),
    "generate_file": ToolSpec(
        name="generate_file",
        description=(
            "Generate and save a file. Formats: markdown, csv, xlsx, docx, pdf. "
            "File is saved to the workspace directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "format":   {"type": "string", "description": "markdown|csv|xlsx|docx|pdf"},
                "filename": {"type": "string", "description": "Output filename with extension"},
                "content":  {"type": "string", "description": "Text content (markdown/docx/pdf)"},
                "title":    {"type": "string", "description": "Document title"},
                "rows":     {"type": "array",  "description": "Row data for CSV (list of objects)"},
                "sheets":   {"type": "object", "description": "Sheet data for XLSX {sheetName: [rows]}"},
                "subdir":   {"type": "string", "description": "Subdirectory within workspace"},
            },
            "required": ["format", "filename"],
        },
        fn=tool_generate_file,
    ),
}


def execute_tool(name: str, args: dict) -> ToolResult:
    """Execute a tool by name. Returns ToolResult with success/error."""
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return ToolResult(
            success=False, output="",
            error=f"Unknown tool: {name!r}. Available: {list(TOOL_REGISTRY)}",
        )
    log.info("Tool call", tool=name, args={k: str(v)[:80] for k, v in args.items()})
    try:
        result = spec.fn(args)
        log.info("Tool result", tool=name, success=result.success,
                 output_len=len(result.output))
        return result
    except Exception as exc:
        log.error("Tool raised exception", tool=name, error=str(exc))
        return ToolResult(success=False, output="", error=str(exc))


def get_tool_schemas_openai(names: Optional[List[str]] = None) -> List[dict]:
    """Return OpenAI-format tool schemas for the given tool names (or all)."""
    specs = [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY] \
            if names else list(TOOL_REGISTRY.values())
    return [s.to_openai_schema() for s in specs]


def get_tool_schemas_anthropic(names: Optional[List[str]] = None) -> List[dict]:
    """Return Anthropic-format tool schemas for the given tool names (or all)."""
    specs = [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY] \
            if names else list(TOOL_REGISTRY.values())
    return [s.to_anthropic_schema() for s in specs]