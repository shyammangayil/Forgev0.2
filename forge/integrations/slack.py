"""
FORGE Slack Integration
=======================
Send messages, read channels, and manage notifications via Slack Web API.

Env vars
--------
  SLACK_BOT_TOKEN    Bot User OAuth Token (xoxb-...)
  SLACK_CHANNEL      default channel for notifications (e.g. #forge-notifications)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from forge.telemetry.logger import get_logger

log = get_logger("slack")

_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")
_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
_TIMEOUT = 15
_API     = "https://slack.com/api"


class SlackError(Exception):
    pass


def _request(method: str, body: dict) -> dict:
    if not _TOKEN:
        raise SlackError("SLACK_BOT_TOKEN not set")
    url = f"{_API}/{method}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise SlackError(f"Network error: {e}")

    if not result.get("ok"):
        raise SlackError(f"Slack API error: {result.get('error', 'unknown')}")
    return result


# ── Messages ──────────────────────────────────────────────────────────────────

def send_message(
    text: str,
    channel: str = "",
    blocks: Optional[List[dict]] = None,
    thread_ts: Optional[str] = None,
) -> dict:
    """Send a message to a channel."""
    ch = channel or _CHANNEL
    if not ch:
        raise SlackError("No channel specified and SLACK_CHANNEL not set")

    body: dict = {"channel": ch, "text": text}
    if blocks:
        body["blocks"] = blocks
    if thread_ts:
        body["thread_ts"] = thread_ts

    result = _request("chat.postMessage", body)
    log.info("Slack message sent", channel=ch)
    return result


def send_rich_message(
    channel: str,
    title: str,
    text: str,
    color: str = "#36a64f",
    fields: Optional[List[Dict[str, str]]] = None,
) -> dict:
    """Send a message with a formatted attachment."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
    ]
    if fields:
        field_blocks = [
            {"type": "mrkdwn", "text": f"*{f['title']}*\n{f['value']}"}
            for f in fields
        ]
        # Add fields in pairs
        for i in range(0, len(field_blocks), 2):
            blocks.append({
                "type": "section",
                "fields": field_blocks[i:i+2],
            })

    return send_message(text=title, channel=channel, blocks=blocks)


def update_message(channel: str, ts: str, text: str) -> dict:
    """Update an existing message."""
    return _request("chat.update", {"channel": channel, "ts": ts, "text": text})


def delete_message(channel: str, ts: str) -> dict:
    """Delete a message."""
    return _request("chat.delete", {"channel": channel, "ts": ts})


# ── Channels ──────────────────────────────────────────────────────────────────

def list_channels(limit: int = 50) -> List[dict]:
    """List public channels."""
    result = _request("conversations.list", {
        "limit": limit,
        "types": "public_channel,private_channel",
    })
    return result.get("channels", [])


def get_channel_history(channel: str, limit: int = 10) -> List[dict]:
    """Get recent messages from a channel."""
    result = _request("conversations.history", {
        "channel": channel,
        "limit": limit,
    })
    return result.get("messages", [])


def get_thread_replies(channel: str, thread_ts: str) -> List[dict]:
    """Get replies in a thread."""
    result = _request("conversations.replies", {
        "channel": channel,
        "ts": thread_ts,
    })
    return result.get("messages", [])


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user_info(user_id: str) -> dict:
    """Get user information."""
    return _request("users.info", {"user": user_id})


def send_dm(user_id: str, text: str) -> dict:
    """Send a direct message to a user."""
    # Open DM channel first
    result = _request("conversations.open", {"users": user_id})
    channel = result["channel"]["id"]
    return send_message(text=text, channel=channel)


# ── Forge-specific notifications ──────────────────────────────────────────────

def notify_task_complete(
    task_id: str,
    task_title: str,
    success: bool,
    duration_minutes: float,
    channel: str = "",
) -> None:
    """Send a task completion notification."""
    ch = channel or _CHANNEL
    if not ch:
        return  # silently skip if no channel configured

    icon = "✅" if success else "❌"
    status = "completed" if success else "failed"
    color = "#36a64f" if success else "#d9534f"

    try:
        send_rich_message(
            channel=ch,
            title=f"{icon} Forge Task {status.title()}",
            text=f"*{task_title}*",
            color=color,
            fields=[
                {"title": "Task ID", "value": task_id},
                {"title": "Status", "value": status.title()},
                {"title": "Duration", "value": f"{duration_minutes:.1f} min"},
            ],
        )
    except SlackError as e:
        log.warning("Slack notification failed", error=str(e))


def notify_task_failed(
    task_id: str,
    task_title: str,
    phase: str,
    error: str,
    channel: str = "",
) -> None:
    """Send a task failure notification with error details."""
    ch = channel or _CHANNEL
    if not ch:
        return

    try:
        send_rich_message(
            channel=ch,
            title="❌ Forge Task Failed",
            text=f"*{task_title}*\nFailed at phase: `{phase}`",
            color="#d9534f",
            fields=[
                {"title": "Task ID", "value": task_id},
                {"title": "Failed Phase", "value": phase},
                {"title": "Error", "value": error[:200]},
            ],
        )
    except SlackError as e:
        log.warning("Slack notification failed", error=str(e))