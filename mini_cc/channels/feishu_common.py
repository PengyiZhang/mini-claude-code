"""Shared helpers for both Feishu channel implementations.

Both the webhook implementation (``feishu.py``) and the WS long-connection
implementation (``feishu_ws.py``) need to:

* parse an ``im.message.receive_v1`` event payload into a
  ``(user_input, metadata)`` pair,
* strip ``@_user_N`` bot mentions from message text,
* render outbound events (text / teammate_message / lead_nudged /
  tool_result) as plain Feishu text,
* refresh + cache the tenant_access_token under a lock.

Centralising here keeps each implementation file focused on its transport.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False


_FEISHU_OPEN_BASE = "https://open.feishu.cn"
_TOKEN_REFRESH_MARGIN = 60.0
_HTTP_TIMEOUT = 5.0


# Cross-instance cache of last-seen inbound chat_id, keyed by binding.id.
# Necessary because the channel dispatcher builds a fresh Channel
# instance per outbound deliver() (see base.py:ChannelDispatcher._dispatch
# → registry.get_channel → factory(binding)) — that fresh instance would
# otherwise never see the chat_id the supervisor's long-lived receiver
# instance captured. A module-level dict is the lightest shared bus.
# Tuple of (chat_id, ts) so we could expire if needed; for now just last.
_INBOUND_CHAT_IDS: dict[str, str] = {}


def remember_inbound_chat_id(binding_id: str, chat_id: str) -> None:
    """Record the most recent inbound chat_id for ``binding_id``. Safe to
    call from any thread (Python dict atomic set/get)."""
    if chat_id:
        _INBOUND_CHAT_IDS[binding_id] = chat_id


def lookup_inbound_chat_id(binding_id: str) -> str | None:
    """Return the last-seen inbound chat_id for ``binding_id``, or None."""
    return _INBOUND_CHAT_IDS.get(binding_id)


# Pre-compiled: Feishu embeds @bot as `@_user_1` tokens in the text body
# (with the actual mention in a separate field). Strip them so the LLM
# doesn't see them.
_MENTION_RE = re.compile(r"@_user_\d+")


def strip_bot_mention(text: str) -> str:
    """Remove ``@_user_N`` bot-mention tokens from ``text`` and trim."""
    return _MENTION_RE.sub("", text).strip()


def parse_message_event(event: dict) -> tuple[str | None, dict]:
    """Extract ``(user_input, metadata)`` from an
    ``im.message.receive_v1`` event payload. Returns ``(None, {})`` when
    the message should be dropped (non-text, empty body after mention
    strip, etc.). Reused by both webhook and WS paths.

    ``metadata`` always carries ``source="feishu"``, ``sender_open_id``,
    ``chat_id``, ``message_id`` so the agent context can attribute the
    message."""
    sender = event.get("sender") or {}
    sender_id = (sender.get("sender_id") or {}).get("open_id", "?")
    message = event.get("message") or {}
    msg_type = message.get("message_type", "")
    chat_id = message.get("chat_id", "")

    if msg_type != "text":
        # Non-text: inject a placeholder so the lead sees the user tried,
        # rather than silently dropping. Keeps chat_id+message_id visible.
        return (
            f"[Feishu non-text message: type={msg_type}]",
            {
                "source": "feishu",
                "sender_open_id": sender_id,
                "chat_id": chat_id,
                "message_id": message.get("message_id", ""),
            },
        )
    content_raw = message.get("content", "{}")
    try:
        content = json.loads(content_raw) if content_raw else {}
    except json.JSONDecodeError:
        content = {}
    text = str(content.get("text", "")).strip()
    if not text:
        return None, {}
    text = strip_bot_mention(text)
    if not text:
        return None, {}
    return text, {
        "source": "feishu",
        "sender_open_id": sender_id,
        "chat_id": chat_id,
        "message_id": message.get("message_id", ""),
    }


def render_event(event: dict) -> str:
    """Flatten a session event into a single Feishu text payload. Returns
    empty string when the event shouldn't be pushed (non-text / tool
    noise / etc.) so the caller can short-circuit."""
    etype = event.get("type", "")
    if etype == "text":
        return str(event.get("text", "")).strip()
    if etype == "teammate_message":
        sender = event.get("from", "?")
        content = event.get("content", "")
        return f"[{sender}] {content}".strip()
    if etype == "lead_nudged":
        items = event.get("items") or []
        names = ", ".join(
            f"{i.get('from','?')} ({i.get('kind','?')})" for i in items
        )
        return f"🔔 Lead nudged by: {names}" if names else ""
    if etype == "tool_result":
        content = str(event.get("content", "")).strip()
        if len(content) > 40:
            return f"[tool] {content[:500]}"
    return ""


class TokenCache:
    """Shared tenant_access_token refresh logic. Lock-protected so
    concurrent dispatchers across one channel instance don't fire two
    refresh requests."""

    def __init__(self, app_id: str, app_secret: str,
                 open_base: str = _FEISHU_OPEN_BASE):
        self.app_id = app_id
        self.app_secret = app_secret
        self.open_base = open_base
        self._token = ""
        self._expire = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        """Return a fresh tenant_access_token, refreshing if needed.
        Returns ``""`` on failure (missing creds / network) — caller
        treats as 'skip'."""
        if not self.app_id or not self.app_secret:
            return ""
        with self._lock:
            now = time.time()
            if self._token and now < self._expire - _TOKEN_REFRESH_MARGIN:
                return self._token
            if not _HAS_REQUESTS:
                return ""
            try:
                resp = requests.post(
                    f"{self.open_base}/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.app_id,
                        "app_secret": self.app_secret,
                    },
                    timeout=_HTTP_TIMEOUT,
                )
                data = resp.json() or {}
            except Exception:
                return ""
            token = data.get("tenant_access_token") or ""
            expire = data.get("expire") or 0
            if not token:
                return ""
            self._token = token
            self._expire = now + (float(expire) if expire else 7200.0)
            return token


__all__ = [
    "TokenCache",
    "parse_message_event",
    "render_event",
    "strip_bot_mention",
    "remember_inbound_chat_id",
    "lookup_inbound_chat_id",
    "_FEISHU_OPEN_BASE",
    "_HTTP_TIMEOUT",
]
