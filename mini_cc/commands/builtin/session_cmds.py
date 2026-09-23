"""Session-oriented slash commands (split from commands/registry.py — M3-1)."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterator, Optional

from ...config import default_config
from ...core.llm import looks_like_litellm
from ..cards import (
    CardAction,
    CardBadge,
    CardEvent,
    CardKeyValuePair,
    CardKeyValuePayload,
    CardListItem,
    CardListPayload,
    to_dict,
)
from ..registry import CommandContext, SlashCommand, default_registry

from ._util import _loop_of

def _cmd_help(ctx: CommandContext) -> Iterator[dict]:
    """Yield a list card of available commands. Each row is one command
    with title ``/<name>``, description subtitle, and a badge per alias
    so users can see at a glance that ``/?`` is the same as ``/help``."""
    reg = default_registry()
    items: list[CardListItem] = []
    for c in reg.all_visible():
        badges: list[CardBadge] = []
        for a in c.aliases:
            badges.append(CardBadge(text=f"/{a}", tone="default"))
        items.append(CardListItem(
            id=f"cmd:{c.name}",
            title=f"/{c.name}",
            subtitle=c.description,
            icon="help",
            badges=badges,
            menu=[],
        ))
    items.sort(key=lambda it: it.title)
    card = CardEvent(
        id="help",
        variant="list",
        title="Available commands",
        icon="help",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=f"{len(items)} command{'s' if len(items) != 1 else ''} · type / in the input to see the menu",
            empty_hint=None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_clear(ctx: CommandContext) -> Iterator[dict]:
    """Drop the in-memory transcript for this session and persist the
    empty state. On-disk history is replaced too so a refresh doesn't
    bring the cleared messages back."""
    sm = ctx.session_manager
    if sm is None:
        yield {"type": "error", "message": "session manager unavailable"}
        return
    try:
        sess = sm._sessions.get((ctx.project_id, ctx.session_id))
        if sess is not None and hasattr(sess, "loop"):
            sess.loop.messages.clear()
            sess.loop.todos.clear()
            # Persist the empty state so a reload doesn't restore the
            # history we just cleared on the client.
            try:
                ctx.project.storage.save_messages(
                    ctx.project_id, ctx.session_id, sess.loop.messages)
                ctx.project.storage.save_todos(
                    ctx.project_id, ctx.session_id, sess.loop.todos)
            except Exception:
                pass
    except Exception as e:
        yield {"type": "error", "message": f"clear failed: {e}"}
        return
    yield {"type": "text", "text": "🧹 session cleared."}
    yield {"type": "done"}


def _cmd_sessions(ctx: CommandContext) -> Iterator[dict]:
    """List sessions in this project as a list card.

    Each row carries a 'warm'/'cold' badge (in-memory vs disk-only) and
    an 'active' badge for the current session. Clicking a row fires
    ``/resume <sid>`` through the standard slash-command path so the
    user can pick a session and resume it without leaving the chat
    pane.
    """
    sm = ctx.session_manager
    if sm is None or ctx.project is None:
        yield {"type": "error", "message": "project context unavailable"}
        return
    metas = sm.list(ctx.project_id)
    if not metas:
        yield {"type": "text", "text": "_no sessions in this project_"}
        yield {"type": "done"}
        return
    items: list[CardListItem] = []
    for m in metas:
        badges: list[CardBadge] = []
        if m.in_memory:
            badges.append(CardBadge(text="warm", tone="accent"))
        else:
            badges.append(CardBadge(text="cold", tone="default"))
        if m.session_id == ctx.session_id:
            badges.append(CardBadge(text="active", tone="ok"))
        subtitle = f"{m.message_count} msgs · {m.last_active_at}"
        items.append(CardListItem(
            id=m.session_id,
            title=m.session_id,
            subtitle=subtitle,
            icon="sessions",
            badges=badges,
            expandable_command=f"/resume {m.session_id}",
            menu=[],
        ))
    card = CardEvent(
        id="sessions",
        variant="list",
        title=f"Sessions · {ctx.project_id}",
        icon="sessions",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=f"{len(items)} session{'s' if len(items) != 1 else ''}",
            empty_hint=None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_model(ctx: CommandContext) -> Iterator[dict]:
    """Show current model + provider backend as a key_value card.

    Surfaces the config-default model, the resolved backend (anthropic
    vs litellm), the fallback, and any per-session override so the
    user can tell "I changed it via the API" from "this is the global
    default" at a glance.
    """
    cfg = default_config()
    model = cfg.primary_model
    backend = "litellm" if looks_like_litellm(model) else "anthropic"
    fallback = cfg.fallback_model or "—"
    sess_override = getattr(getattr(_loop_of(ctx), "state", None),
                            "current_model", None) or "—"
    pairs = [
        CardKeyValuePair(k="model", v=model, mono=True),
        CardKeyValuePair(k="backend", v=backend, mono=True),
        CardKeyValuePair(k="fallback", v=fallback, mono=True),
        CardKeyValuePair(k="session override", v=sess_override, mono=True),
    ]
    card = CardEvent(
        id="model",
        variant="key_value",
        title="Current model",
        icon="model",
        status="ok",
        payload=CardKeyValuePayload(pairs=pairs).__dict__,
        actions=[
            CardAction(label="↻ refresh", command="/model", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_search(ctx: CommandContext) -> Iterator[dict]:
    """F3.1: substring search across every session in the project,
    rendered as a list card. Each hit row carries ``/resume <sid>`` as
    its expandable command so a click opens the matching session."""
    query = (ctx.args or "").strip()
    storage = ctx.storage
    if storage is None and ctx.project is not None:
        storage = getattr(ctx.project, "storage", None)

    # Build empty-state card helper. We emit a card even when the query
    # is blank or there are no matches so the UI keeps a consistent
    # shape (card with empty_hint) instead of falling back to a text
    # bubble that breaks the visual rhythm of the chat.
    def _empty_card(hint: str) -> dict:
        return to_dict(CardEvent(
            id="search",
            variant="list",
            title=f"Search · `{query or '—'}`",
            icon="search",
            status="ok",
            payload=CardListPayload(
                items=[],
                summary=None,
                empty_hint=hint,
            ).__dict__,
        ))

    if not query:
        yield _empty_card("type a query in the form `/search <query>` to scan every session in this project.")
        yield {"type": "done"}
        return
    if storage is None:
        yield to_dict(CardEvent(
            id="search",
            variant="list",
            title=f"Search · `{query}`",
            icon="search",
            status="error",
            error_message="storage unavailable",
            payload=CardListPayload(items=[], empty_hint=None).__dict__,
        ))
        yield {"type": "done"}
        return
    try:
        hits = storage.search_messages(ctx.project_id, query, limit=20)
    except Exception as e:
        yield to_dict(CardEvent(
            id="search",
            variant="list",
            title=f"Search · `{query}`",
            icon="search",
            status="error",
            error_message=f"{type(e).__name__}: {e}",
            payload=CardListPayload(items=[], empty_hint=None).__dict__,
        ))
        yield {"type": "done"}
        return
    if not hits:
        yield _empty_card(f"no matches for `{query}`.")
        yield {"type": "done"}
        return
    items: list[CardListItem] = []
    for h in hits:
        items.append(CardListItem(
            id=f"{h.session_id}:{h.message_index}",
            title=h.session_id,
            subtitle=h.snippet,
            icon="search",
            badges=[CardBadge(text=h.role, tone="default")],
            meta=f"msg #{h.message_index + 1}",
            expandable_command=f"/resume {h.session_id}",
            menu=[],
        ))
    card = CardEvent(
        id="search",
        variant="list",
        title=f"Search · `{query}`",
        icon="search",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=f"{len(hits)} match{'es' if len(hits) != 1 else ''}",
            empty_hint=None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_export(ctx: CommandContext) -> Iterator[dict]:
    """F3.2: export this session to markdown or JSON.

    Output lands in the chat (so the user can copy) AND is persisted to
    ``<workspace>/.mini_cc/exports/<sid>.<ext>`` so it can be retrieved
    via the fs tools / downloaded from the file tree.
    """
    import json as _json
    from pathlib import Path
    fmt = (ctx.args or "").strip().lower() or "md"
    if fmt not in ("md", "json"):
        yield {"type": "text",
               "text": f"Unknown format `{fmt}`. Use `/export md` or `/export json`."}
        yield {"type": "done"}
        return
    storage = ctx.storage
    if storage is None and ctx.project is not None:
        storage = ctx.project.storage
    if storage is None:
        yield {"type": "text", "text": "Error: storage unavailable."}
        yield {"type": "done"}
        return
    try:
        msgs = storage.load_messages(ctx.project_id, ctx.session_id)
    except Exception as e:
        yield {"type": "text", "text": f"Error loading session: {e}"}
        yield {"type": "done"}
        return
    if not msgs:
        yield {"type": "text", "text": "Session is empty — nothing to export."}
        yield {"type": "done"}
        return
    if fmt == "json":
        body = _json.dumps(msgs, ensure_ascii=False, indent=2, default=str)
    else:
        body = _render_session_markdown(ctx.session_id, msgs)
    # Persist to workspace exports dir for retrieval.
    ws = (ctx.project.workspace if ctx.project is not None else None)
    saved_to = ""
    if ws is not None:
        try:
            exports = Path(ws) / ".mini_cc" / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            fp = exports / f"{ctx.session_id}.{fmt}"
            fp.write_text(body, encoding="utf-8")
            saved_to = f"\n\nSaved to `{fp}`."
        except OSError as e:
            saved_to = f"\n\n(warn: could not save file: {e})"
    preview = body if len(body) < 4000 else body[:4000] + "\n... (truncated preview)"
    yield {"type": "text",
           "text": f"```{fmt}\n{preview}\n```{saved_to}"}
    yield {"type": "done"}


def _render_session_markdown(session_id: str, msgs: list[dict]) -> str:
    """Render an Anthropic-shaped message list as readable markdown."""
    import json as _json
    out = [f"# Session {session_id}", ""]
    for m in msgs:
        role = m.get("role", "unknown")
        content = m.get("content")
        if isinstance(content, str):
            out.append(f"## {role}\n\n{content}\n")
            continue
        if not isinstance(content, list):
            continue
        chunks: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                chunks.append(b.get("text", ""))
            elif t == "tool_use":
                inp = _json.dumps(b.get("input", {}), ensure_ascii=False)
                chunks.append(f"**tool_use `{b.get('name', '')}`:**\n```json\n{inp}\n```")
            elif t == "tool_result":
                inner = b.get("content")
                if isinstance(inner, str):
                    text = inner
                elif isinstance(inner, list):
                    text = "\n".join(str(ib.get("text", "")) for ib in inner
                                     if isinstance(ib, dict) and ib.get("type") == "text")
                else:
                    text = str(inner)
                chunks.append(f"**tool_result:**\n```\n{text}\n```")
        if chunks:
            out.append(f"## {role}\n\n" + "\n\n".join(chunks) + "\n")
    return "\n".join(out)


def _cmd_fork(ctx: CommandContext) -> Iterator[dict]:
    """F3.3: branch this session into a new one with the transcript copied.

    Creates a new session under the same project, copies all messages from
    the active session into it, and warms it via SessionManager. Emits a
    `session_resumed` event so the client rotates its active session id to
    the fork.
    """
    sm = ctx.session_manager
    storage = ctx.storage or getattr(ctx.project, "storage", None)
    if sm is None or storage is None:
        yield {"type": "error", "message": "project/storage unavailable"}
        return
    try:
        src = storage.load_messages(ctx.project_id, ctx.session_id)
    except Exception as e:
        yield {"type": "error", "message": f"load source failed: {e}"}
        return
    if not src:
        yield {"type": "text",
               "text": "_source session is empty — nothing to fork. "
                       "Send a message first._"}
        yield {"type": "done"}
        return
    import uuid as _uuid
    new_sid = f"sess_{_uuid.uuid4().hex[:8]}"
    try:
        storage.save_messages(ctx.project_id, new_sid, list(src))
    except Exception as e:
        yield {"type": "error", "message": f"persist fork failed: {e}"}
        return
    try:
        sm.start_session(ctx.project_id, new_sid)
    except Exception as e:
        yield {"type": "error", "message": f"warm fork failed: {e}"}
        return
    yield {"type": "session_resumed",
           "project_id": ctx.project_id, "session_id": new_sid}
    yield {"type": "text",
           "text": (f"🌱 forked `{ctx.session_id}` → `{new_sid}` "
                    f"({len(src)} messages copied). "
                    f"You're now in the new branch.")}
    yield {"type": "done"}


def _cmd_resume(ctx: CommandContext) -> Iterator[dict]:
    """Resume (or list) a session in this project.

    With no args, lists sessions in last-active order with a hint that
    `/resume <id>` switches the active session. With an id, warms that
    session via the SessionManager so subsequent sends reuse its on-disk
    transcript.

    The client owns which session_id is "active" in the UI; this command
    warms the backend side and emits a `session_resumed` event so the
    client can rotate its active session id in response.
    """
    sm = ctx.session_manager
    if sm is None:
        yield {"type": "error", "message": "session manager unavailable"}
        return
    args = (ctx.args or "").strip().split()
    if not args:
        try:
            metas = sm.list(ctx.project_id)
        except Exception as e:
            yield {"type": "error", "message": f"list failed: {e}"}
            return
        if not metas:
            yield {"type": "text",
                   "text": "_no sessions in this project yet_"}
            yield {"type": "done"}
            return
        lines = [f"**Recent sessions in `{ctx.project_id}`:**", ""]
        for m in metas[:10]:
            marker = " ← active" if m.session_id == ctx.session_id else ""
            warm = "🟢" if m.in_memory else "⚪"
            when = (m.last_active_at or "")[:19]
            lines.append(f"- {warm} `{m.session_id}` · "
                         f"{m.message_count} msgs · {when}{marker}")
        lines.append("")
        lines.append("Resume with `/resume <session_id>`")
        yield {"type": "text", "text": "\n".join(lines)}
        yield {"type": "done"}
        return

    target = args[0]
    try:
        # `start_session` is idempotent: warms an on-disk session back to
        # memory, or errors via KeyError-equivalent when the id is unknown.
        existing = {m.session_id for m in sm.list(ctx.project_id)}
    except Exception as e:
        yield {"type": "error", "message": f"list failed: {e}"}
        return
    if target not in existing:
        yield {"type": "error",
               "message": f"session `{target}` not found in project `{ctx.project_id}`"}
        return
    try:
        sess = sm.start_session(ctx.project_id, target)
    except Exception as e:
        yield {"type": "error", "message": f"resume failed: {e}"}
        return
    msg_count = len(getattr(getattr(sess, "loop", None), "messages", []) or [])
    yield {"type": "session_resumed",
           "project_id": ctx.project_id, "session_id": target}
    yield {"type": "text",
           "text": f"🔄 resumed session `{target}` — "
                   f"{msg_count} messages replayed from disk"}
    yield {"type": "done"}


# ── Default registry ───────────────────────────────────────────────────────

_DEFAULT: Optional[CommandRegistry] = None



def register(reg) -> None:

    reg.register(SlashCommand(
        name="help",
        description="Show this list of commands.",
        aliases=("?",),
        handler=_cmd_help,
    ))
    reg.register(SlashCommand(
        name="clear",
        description="Clear the active session's chat history (in-memory + on-disk).",
        aliases=("cls",),
        handler=_cmd_clear,
    ))
    reg.register(SlashCommand(
        name="sessions",
        description="List all sessions in the current project.",
        handler=_cmd_sessions,
    ))
    reg.register(SlashCommand(
        name="model",
        description="Show the current model + provider backend.",
        handler=_cmd_model,
    ))
    reg.register(SlashCommand(
        name="resume",
        description="List recent sessions, or resume one by id: `/resume <session_id>`.",
        handler=_cmd_resume,
    ))
    reg.register(SlashCommand(
        name="search",
        description=("Search every session's messages in this project for a "
                     "keyword. `/search login` returns matching snippets with "
                     "session ids. Use `/resume <id>` to open one."),
        handler=_cmd_search,
    ))
    reg.register(SlashCommand(
        name="fork",
        description=("Branch this session: copy its transcript to a new "
                     "session id and switch to it. Original is preserved."),
        handler=_cmd_fork,
    ))
    reg.register(SlashCommand(
        name="export",
        description=("Export this session to markdown or JSON. `/export md` "
                     "(default) or `/export json`. Output is appended to chat "
                     "and saved to <workspace>/.mini_cc/exports/<sid>.<ext>."),
        handler=_cmd_export,
    ))
    _DEFAULT = reg
    return reg
