"""Slash command registry + built-in commands.

Built-ins:

- ``/help``      — list available commands (server)
- ``/clear``     — wipe the in-memory transcript for the active session (server)
- ``/sessions``  — list sessions in this project (server)
- ``/model``     — show current model + provider (server)
- ``/compact``   — manually trigger history compaction (server)

All server-scoped handlers return an iterator of SSE-shaped dicts (same
shape as ``AgentLoop.run`` yields) so the frontend can render them in
the chat pane uniformly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal, Optional

from ..config import default_config
from ..core.llm import looks_like_litellm
from .cards import (
    CardAction,
    CardBadge,
    CardEvent,
    CardKeyValuePair,
    CardKeyValuePayload,
    CardListItem,
    CardListPayload,
    to_dict,
)


@dataclass
class CommandContext:
    """Context passed to a server-side command handler.

    Holds everything a built-in like ``/sessions`` needs to produce its
    output without reaching into the FastAPI app state directly.
    """
    project_id: str
    session_id: str
    tenant_id: str
    args: str = ""                       # raw text after the command name
    project: Any = None                  # Project (from ProjectManager)
    session_manager: Any = None          # SessionManager
    storage: Any = None                  # Storage


@dataclass
class SlashCommand:
    """A single command definition.

    - ``scope="client"``  → frontend handles it; ``handler`` is None.
    - ``scope="server"``  → backend runs ``handler(ctx) -> Iterator[dict]``
      yielding SSE events ({type:"text"|"done"|...}).
    """
    name: str
    description: str
    scope: Literal["client", "server"] = "server"
    aliases: tuple[str, ...] = ()
    handler: Optional[Callable[[CommandContext], Iterator[dict]]] = None
    visible: bool = True                 # set False for hidden aliases


class CommandRegistry:
    """In-memory registry. Add commands with :meth:`register`, look up
    with :meth:`resolve`, enumerate visible ones with :meth:`all_visible`."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        if cmd.name in self._commands:
            raise ValueError(f"command already registered: {cmd.name}")
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            # Alias is registered as a hidden pointer back to the same
            # SlashCommand so resolve() can find it transparently.
            self._commands[alias] = SlashCommand(
                name=alias,
                description=cmd.description,
                scope=cmd.scope,
                handler=cmd.handler,
                visible=False,
            )

    def resolve(self, name: str) -> Optional[SlashCommand]:
        # Strip a leading slash so callers can pass either "/help" or "help".
        if name.startswith("/"):
            name = name[1:]
        return self._commands.get(name)

    def all_visible(self) -> list[SlashCommand]:
        return sorted(
            (c for c in self._commands.values() if c.visible),
            key=lambda c: c.name,
        )


# ── Built-in handlers ──────────────────────────────────────────────────────

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


def _cmd_skills(ctx: CommandContext) -> Iterator[dict]:
    """List skills available in this project as a list card.

    Each row is one discovered skill; subtitle is the first line of its
    SKILL.md description so users can scan the catalog at a glance.
    """
    project = ctx.project
    if project is None or project.skills_loader is None:
        yield {"type": "error", "message": "skills not configured for this project"}
        return
    try:
        project.skills_loader.scan()
    except Exception:
        pass
    reg = project.skills_loader.registry
    items: list[CardListItem] = []
    for s in reg.values():
        desc = (s.description or "").strip().splitlines()[0] if s.description else ""
        items.append(CardListItem(
            id=s.name,
            title=s.name,
            subtitle=desc or None,
            icon="skills",
            badges=[],
            menu=[],
        ))
    card = CardEvent(
        id="skills",
        variant="list",
        title=f"Skills · {ctx.project_id}",
        icon="skills",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=f"{len(items)} skill{'s' if len(items) != 1 else ''}" if items else None,
            empty_hint="no skills found. Drop one into `<workspace>/skills/<name>/SKILL.md` and re-run `/skills`." if not items else None,
        ).__dict__,
        actions=[CardAction(label="↻ rescan", command="/skills", tone="default")],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_tools(ctx: CommandContext) -> Iterator[dict]:
    """List every tool the agent can call this turn, as a list card.

    Each row is tagged ``builtin`` or ``mcp`` so the user can tell at a
    glance where a tool came from — useful when debugging "why doesn't
    the agent see my MCP tool" vs "this builtin needs an API key".
    """
    from ..tools import builtin_tools
    project = ctx.project
    builtin = sorted(t.name for t in builtin_tools())
    mcp_names: list[str] = []
    if project is not None and project.mcp_pool is not None:
        try:
            for t in project.mcp_pool.all_tools():
                mcp_names.append(t.name)
        except Exception:
            pass
    mcp_names = sorted(set(mcp_names))
    items: list[CardListItem] = []
    for name in builtin:
        items.append(CardListItem(
            id=f"builtin:{name}",
            title=name,
            icon="tools",
            badges=[CardBadge(text="builtin", tone="default")],
            menu=[],
        ))
    for name in mcp_names:
        items.append(CardListItem(
            id=f"mcp:{name}",
            title=name,
            icon="tools",
            badges=[CardBadge(text="mcp", tone="accent")],
            menu=[],
        ))
    summary = f"{len(builtin)} builtin · {len(mcp_names)} mcp"
    card = CardEvent(
        id="tools",
        variant="list",
        title=f"Tools · {ctx.project_id}",
        icon="tools",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=summary,
            empty_hint=None,
        ).__dict__,
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


def _cmd_mcp(ctx: CommandContext) -> Iterator[dict]:
    """MCP server management, aligned with Claude Code's /mcp.

    Subcommands (debug.7.md Task 2):
    - no args: roster card (connected / failed / available)
    - ``tools [server]``: list tools, optionally filtered to one server
    - ``connect <name>``: invoke pool.connect(name)
    - ``disconnect <name>``: invoke pool.disconnect(name)
    - ``reconnect <name>``: disconnect then connect
    """
    project = ctx.project
    if project is None or project.mcp_pool is None:
        yield {"type": "error", "message": "MCP not configured for this project"}
        yield {"type": "done"}
        return
    pool = project.mcp_pool
    parts = (ctx.args or "").split()
    if parts:
        sub = parts[0].lower()
        if sub == "tools":
            yield from _mcp_tools(pool, parts[1] if len(parts) > 1 else None,
                                  ctx.project_id)
            return
        if sub in ("connect", "disconnect", "reconnect") and len(parts) >= 2:
            yield from _mcp_lifecycle(pool, sub, parts[1])
            return
        if sub in ("connect", "disconnect", "reconnect"):
            yield {"type": "error",
                   "message": f"usage: `/mcp {sub} <name>`"}
            yield {"type": "done"}
            return
        yield {"type": "error",
               "message": (f"unknown subcommand '{sub}'. "
                           "Use `/mcp`, `/mcp tools [server]`, "
                           "`/mcp connect <name>`, "
                           "`/mcp disconnect <name>`, or "
                           "`/mcp reconnect <name>`.")}
        yield {"type": "done"}
        return
    yield from _mcp_roster(pool, ctx.project_id)


def _mcp_roster(pool, project_id: str) -> Iterator[dict]:
    """Default `/mcp` roster card (the original behaviour)."""
    try:
        connected = list(pool.list_connected())
    except Exception:
        connected = list(getattr(pool, "_clients", {}).keys())
    try:
        _fn = getattr(pool, "available_servers", None)
        available = list(_fn()) if callable(_fn) else []
    except Exception:
        available = []
    connectable = sorted(s for s in available if s not in connected)
    try:
        attempts = pool.list_attempts()
    except Exception:
        attempts = {}
    failed = sorted(
        name for name, rec in attempts.items()
        if not getattr(rec, "ok", False) and name not in connected)
    items: list[CardListItem] = []
    for name in connected:
        client = getattr(pool, "_clients", {}).get(name)
        tool_count = len(getattr(client, "tools", []) or [])
        items.append(CardListItem(
            id=f"mcp:{name}",
            title=name,
            subtitle=f"{tool_count} tools live",
            icon="mcp",
            badges=[CardBadge(text="connected", tone="ok")],
            menu=[
                CardAction(label="tools",
                           command=f"/mcp tools {name}", tone="default"),
                CardAction(label="reconnect",
                           command=f"/mcp reconnect {name}", tone="default"),
                CardAction(label="disconnect",
                           command=f"/mcp disconnect {name}", tone="err"),
            ],
        ))
    for name in failed:
        reason = getattr(attempts.get(name), "message", "") or "failed"
        items.append(CardListItem(
            id=f"mcp:{name}",
            title=name,
            subtitle=reason,
            icon="mcp",
            badges=[CardBadge(text="failed", tone="err")],
            menu=[
                CardAction(label="reconnect",
                           command=f"/mcp reconnect {name}", tone="accent"),
            ],
        ))
    for name in connectable:
        items.append(CardListItem(
            id=f"mcp:{name}",
            title=name,
            subtitle="available — click connect",
            icon="mcp",
            badges=[CardBadge(text="available", tone="default")],
            menu=[
                CardAction(label="connect",
                           command=f"/mcp connect {name}", tone="accent"),
            ],
        ))
    summary_bits: list[str] = []
    if connected:
        summary_bits.append(f"{len(connected)} connected")
    if failed:
        summary_bits.append(f"{len(failed)} failed")
    if connectable:
        summary_bits.append(f"{len(connectable)} available")
    card = CardEvent(
        id="mcp",
        variant="list",
        title=f"MCP servers · {project_id}",
        icon="mcp",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=" · ".join(summary_bits) if items else None,
            empty_hint="no MCP servers registered. Drop a `.mcp.json` into any `.mini_cc/` tier, or register a factory at app startup via `MCPPool.register_factory(name, fn)`." if not items else None,
        ).__dict__,
        actions=[
            CardAction(label="＋ tools", command="/mcp tools", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _mcp_tools(pool, server: str | None, project_id: str) -> Iterator[dict]:
    """``/mcp tools [server]`` — list tools from connected servers.

    Server filter narrows to one named server; an unknown server name
    emits an empty-state hint rather than a confusing empty list.
    """
    connected = list(pool.list_connected())
    if server is not None and server not in connected:
        card = CardEvent(
            id="mcp-tools",
            variant="list",
            title=f"MCP tools · {project_id}",
            icon="mcp",
            status="ok",
            payload=CardListPayload(
                items=[],
                empty_hint=(f"no such server '{server}' (or not connected). "
                            "Use `/mcp` to see connected servers."),
            ).__dict__,
        )
        yield to_dict(card)
        yield {"type": "done"}
        return
    clients_dict = getattr(pool, "_clients", {})
    items: list[CardListItem] = []
    for srv in connected:
        if server is not None and srv != server:
            continue
        client = clients_dict.get(srv)
        tools = getattr(client, "tools", []) or []
        for t in tools:
            tname = t.get("name", "?")
            desc = (t.get("description", "") or "").strip()
            if len(desc) > 90:
                desc = desc[:87] + "…"
            items.append(CardListItem(
                id=f"mcp-tool:{srv}:{tname}",
                title=tname,
                subtitle=desc or None,
                icon="mcp",
                badges=[CardBadge(text=srv, tone="default")],
                menu=[],
            ))
    summary = (f"{len(items)} tool{'s' if len(items) != 1 else ''}"
               if items else None)
    card = CardEvent(
        id="mcp-tools",
        variant="list",
        title=f"MCP tools · {project_id}",
        icon="mcp",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=summary,
            empty_hint=("no MCP tools available — connect a server via "
                        "`/mcp connect <name>` first.")
            if not items else None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _mcp_lifecycle(pool, action: str, name: str) -> Iterator[dict]:
    """``/mcp connect|disconnect|reconnect <name>`` lifecycle op."""
    if action == "connect":
        try:
            ok, msg = pool.connect(name)
        except Exception as e:
            yield {"type": "error",
                   "message": f"connect failed: {e}"}
            yield {"type": "done"}
            return
        if not ok:
            yield {"type": "error", "message": msg}
            yield {"type": "done"}
            return
        yield {"type": "text", "text": f"✓ {msg}"}
        yield {"type": "done"}
        return
    if action == "disconnect":
        try:
            ok = pool.disconnect(name)
        except Exception as e:
            yield {"type": "error",
                   "message": f"disconnect failed: {e}"}
            yield {"type": "done"}
            return
        if not ok:
            yield {"type": "error",
                   "message": (f"MCP server '{name}' is not connected "
                               "(nothing to disconnect)")}
            yield {"type": "done"}
            return
        yield {"type": "text", "text": f"✓ disconnected {name}"}
        yield {"type": "done"}
        return
    # reconnect
    try:
        pool.disconnect(name)
    except Exception:
        pass
    try:
        ok, msg = pool.connect(name)
    except Exception as e:
        yield {"type": "error",
               "message": f"reconnect failed: {e}"}
        yield {"type": "done"}
        return
    if not ok:
        yield {"type": "error", "message": msg}
        yield {"type": "done"}
        return
    yield {"type": "text", "text": f"✓ reconnected {name}"}
    yield {"type": "done"}


def _cmd_tasks(ctx: CommandContext) -> Iterator[dict]:
    """List durable tasks created via create_task, as a list card.

    Each row's status badge is tone-coded (done=ok, blocked/pending=
    warn, default otherwise) so the roster reads at a glance.
    """
    if ctx.project is None or ctx.storage is None:
        yield {"type": "error", "message": "project context unavailable"}
        return
    try:
        tasks = ctx.storage.load_tasks(ctx.project_id)
    except Exception:
        tasks = []

    def _tone(status: str) -> str:
        s = (status or "").lower()
        if s in ("done", "completed", "complete"):
            return "ok"
        if s in ("blocked", "pending", "todo"):
            return "warn"
        return "default"

    items: list[CardListItem] = []
    for t in tasks:
        meta_bits: list[str] = []
        if t.owner:
            meta_bits.append(f"owner:{t.owner}")
        if t.worktree:
            meta_bits.append(f"wt:{t.worktree}")
        items.append(CardListItem(
            id=t.id,
            title=t.id,
            subtitle=t.subject,
            icon="tasks",
            badges=[CardBadge(text=t.status, tone=_tone(t.status))],
            meta=" · ".join(meta_bits) if meta_bits else None,
            menu=[],
        ))
    summary_bits: list[str] = []
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    for status, count in by_status.items():
        summary_bits.append(f"{count} {status}")
    card = CardEvent(
        id="tasks",
        variant="list",
        title=f"Tasks · {ctx.project_id}",
        icon="tasks",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=" · ".join(summary_bits) if items else None,
            empty_hint="no tasks in this project" if not items else None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_compact(ctx: CommandContext) -> Iterator[dict]:
    """Force a history compaction right now. The compacted transcript
    is snapshotted first so the user can still recover the full
    conversation if needed."""
    from ..core.compaction import compact_history
    sess_loop = _loop_of(ctx)
    if sess_loop is None:
        yield {"type": "error", "message": "session not warm"}
        return
    try:
        sess_loop._save_transcript(sess_loop.messages[:])
        sess_loop.messages[:] = compact_history(
            sess_loop.messages,
            before_compact=sess_loop._save_transcript)
        sess_loop._persist()
    except Exception as e:
        yield {"type": "error", "message": f"compact failed: {e}"}
        return
    yield {"type": "text",
           "text": f"🗜 history compacted ({len(sess_loop.messages)} messages)."}
    yield {"type": "done"}


def _cmd_cost(ctx: CommandContext) -> Iterator[dict]:
    """Show token usage for the current tenant as a key_value card.

    Pulls from MetricsRegistry (attached to Project.metrics). When no
    registry is wired in we surface a friendly "—" rather than 0 so
    users can tell "no usage yet" from "metrics disabled".
    """
    project = ctx.project
    if project is None or project.metrics is None:
        card = CardEvent(
            id="cost",
            variant="key_value",
            title=f"Cost snapshot · {ctx.tenant_id or 'unknown'}",
            icon="cost",
            status="warning",
            error_message="metrics not configured for this project",
            payload=CardKeyValuePayload(pairs=[]).__dict__,
        )
        yield to_dict(card)
        yield {"type": "done"}
        return
    tenant = project.tenant_id or ctx.tenant_id or "unknown"
    snap = project.metrics.snapshot_for_tenant(tenant)
    # anthropic_tokens_total is registered as a (tenant, kind) counter.
    tokens_family = snap.get("counters", {}).get("anthropic_tokens_total", {})
    by_kind = {s["labels"].get("kind", "?"): s["value"]
               for s in tokens_family.get("series", [])}
    inp = by_kind.get("input", 0)
    out = by_kind.get("output", 0)
    cache_read = by_kind.get("cache_read", 0)
    cache_create = by_kind.get("cache_create", 0)
    total = inp + out + cache_read + cache_create

    # Request counter → outcomes (success / error / cancelled).
    req_family = snap.get("counters", {}).get("anthropic_request_total", {})
    by_status = {s["labels"].get("status", "?"): s["value"]
                 for s in req_family.get("series", [])}

    pairs = [
        CardKeyValuePair(k="input tokens", v=f"{inp:,}", mono=True),
        CardKeyValuePair(k="output tokens", v=f"{out:,}", mono=True),
        CardKeyValuePair(k="cache read", v=f"{cache_read:,}", mono=True),
        CardKeyValuePair(k="cache create", v=f"{cache_create:,}", mono=True),
        CardKeyValuePair(k="total", v=f"{total:,}", mono=True),
    ]
    if by_status:
        outcome_bits = [f"{k}={v}" for k, v in sorted(by_status.items())]
        pairs.append(CardKeyValuePair(k="requests", v=", ".join(outcome_bits), mono=True))

    card = CardEvent(
        id="cost",
        variant="key_value",
        title=f"Cost snapshot · {tenant}",
        icon="cost",
        status="ok",
        payload=CardKeyValuePayload(pairs=pairs).__dict__,
        actions=[
            CardAction(label="↻ refresh", command="/cost", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_permissions(ctx: CommandContext) -> Iterator[dict]:
    """Show the sandbox policy as a key_value card.

    Each blocked pattern gets its own pair (named ``blocked: <name>``)
    so users can scan them in a fixed grid rather than a wrapped comma
    list. Allowed git / env and the global hook deny-lists are surfaced
    as their own pairs too — answering "why was my command blocked"
    without grepping source files.
    """
    project = ctx.project
    sandbox = getattr(project, "sandbox", None) if project else None
    if sandbox is None or not hasattr(sandbox, "policy"):
        yield {"type": "text",
               "text": "_sandbox policy not available for this project_"}
        yield {"type": "done"}
        return
    policy = sandbox.policy

    pairs: list[CardKeyValuePair] = []
    if policy.blocked:
        for name, rx in policy.blocked:
            pairs.append(CardKeyValuePair(
                k=f"blocked: {name}", v=rx, mono=True))
    else:
        pairs.append(CardKeyValuePair(k="blocked", v="— none —", mono=True))

    pairs.append(CardKeyValuePair(
        k="allowed git",
        v=", ".join(sorted(policy.allowed_git)) or "— none —",
        mono=True))
    pairs.append(CardKeyValuePair(
        k="allowed env",
        v=", ".join(sorted(policy.allowed_env)) or "— none —",
        mono=True))

    # Surface the hook-level permission gate too (DENY_LIST / DESTRUCTIVE).
    from ..core.hooks import DENY_LIST, DESTRUCTIVE
    pairs.append(CardKeyValuePair(
        k="hook DENY_LIST",
        v=", ".join(repr(s) for s in DENY_LIST) or "— none —",
        mono=True))
    pairs.append(CardKeyValuePair(
        k="hook DESTRUCTIVE",
        v=", ".join(repr(s) for s in DESTRUCTIVE) or "— none —",
        mono=True))

    card = CardEvent(
        id="permissions",
        variant="key_value",
        title=f"Sandbox policy · {ctx.project_id}",
        icon="permissions",
        status="ok",
        payload=CardKeyValuePayload(pairs=pairs).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_agents(ctx: CommandContext) -> Iterator[dict]:
    """Manage teammates spawned via the teams subsystem.

    With no args: list all known teammates (alive + recently stopped),
    their roles, and inbox sizes.
    With ``stop <name>``: send a shutdown_request to a teammate.
    With ``inbox <name>``: peek the teammate's inbox.
    """
    project = ctx.project
    spawner = getattr(project, "teams", None) if project else None
    if spawner is None:
        yield {"type": "text",
               "text": "_teams subsystem not configured for this project_"}
        yield {"type": "done"}
        return

    parts = (ctx.args or "").split()
    if parts:
        sub = parts[0].lower()
        if sub == "spawn":
            # `/agents spawn <name> <role> --prompt <text>` — deterministic
            # shell entry, mirrors the `spawn_teammate` tool so testers
            # and e2e can drive a spawn without burning LLM tokens.
            err = _parse_spawn_args(ctx.args or "")
            if isinstance(err, str):
                yield {"type": "error", "message": err}
                yield {"type": "done"}
                return
            name, role, prompt = err
            result = spawner.spawn(name, role, prompt)
            if result is not None:
                yield {"type": "error", "message": result}
                yield {"type": "done"}
                return
            yield {"type": "text",
                   "text": f"Teammate '{name}' spawned"}
            yield {"type": "done"}
            return
        if sub == "stop" and len(parts) >= 2:
            name = parts[1]
            try:
                msg = spawner.request_shutdown(name)
            except Exception as e:
                yield {"type": "error", "message": f"shutdown failed: {e}"}
                yield {"type": "done"}
                return
            yield {"type": "text", "text": f"🛑 {msg}"}
            yield {"type": "done"}
            return
        if sub == "inbox" and len(parts) >= 2:
            name = parts[1]
            inbox = _peek_inbox(spawner, name)
            if inbox is None:
                yield {"type": "error",
                       "message": f"no MessageBus or unknown teammate {name!r}"}
                yield {"type": "done"}
                return
            if not inbox:
                yield {"type": "text",
                       "text": f"_`{name}` has an empty inbox._"}
                yield {"type": "done"}
                return
            lines = [f"**Inbox for `{name}` ({len(inbox)}):**", ""]
            for i, m in enumerate(inbox[:10], 1):
                # MessageBus.send writes keys `from`/`type` (teams/__init__.py:57).
                # Older readers used `from_agent`/`kind`, which silently fell
                # back to `?`/`message` for every real message.
                sender = m.get("from") or m.get("from_agent") or "?"
                kind = m.get("type") or m.get("kind") or "message"
                body = (m.get("content") or "").strip()
                if len(body) > 80:
                    body = body[:80] + "…"
                lines.append(f"{i}. _{sender}_ ({kind}): {body}")
            if len(inbox) > 10:
                lines.append(f"\n_… {len(inbox) - 10} more_")
            yield {"type": "text", "text": "\n".join(lines)}
            yield {"type": "done"}
            return
        if sub == "delete" and len(parts) >= 2:
            # debug.7.md Task 1d: drop a *stopped* teammate from the roster.
            # Alive teammates must be stopped first (force-delete races the
            # worker thread).
            name = parts[1]
            if _spawner_is_alive(spawner, name):
                yield {"type": "error",
                       "message": (f"Teammate '{name}' is still alive — "
                                   "stop it first with `/agents stop`")}
                yield {"type": "done"}
                return
            delete = getattr(spawner, "delete", None)
            if not callable(delete):
                yield {"type": "error",
                       "message": "this spawner does not support delete"}
                yield {"type": "done"}
                return
            err = delete(name)
            if err is not None:
                yield {"type": "error", "message": err}
                yield {"type": "done"}
                return
            yield {"type": "text", "text": f"🗑 Teammate '{name}' deleted"}
            yield {"type": "done"}
            return
        if sub == "edit" and len(parts) >= 2:
            # debug.7.md Task 1d: update role/prompt on a stopped teammate.
            name = parts[1]
            if _spawner_is_alive(spawner, name):
                yield {"type": "error",
                       "message": (f"Teammate '{name}' is still alive — "
                                   "stop it before editing")}
                yield {"type": "done"}
                return
            edit = getattr(spawner, "edit", None)
            if not callable(edit):
                yield {"type": "error",
                       "message": "this spawner does not support edit"}
                yield {"type": "done"}
                return
            role, prompt = _parse_edit_args(ctx.args or "")
            if role is None and prompt is None:
                yield {"type": "error",
                       "message": ("usage: `/agents edit <name> "
                                   "[--role <r>] [--prompt <p>]` "
                                   "(at least one flag required)")}
                yield {"type": "done"}
                return
            err = edit(name, role=role, prompt=prompt)
            if err is not None:
                yield {"type": "error", "message": err}
                yield {"type": "done"}
                return
            yield {"type": "text",
                   "text": f"✏ Teammate '{name}' updated"}
            yield {"type": "done"}
            return
        yield {"type": "error",
               "message": f"unknown subcommand '{sub}'. "
                          "Use `/agents`, `/agents spawn ...`, "
                          "`/agents stop <name>`, `/agents inbox <name>`, "
                          "`/agents delete <name>`, or `/agents edit <name> ...`."}
        yield {"type": "done"}
        return
    all_known = list(getattr(spawner, "_teammates", {}).values())
    alive_count = sum(1 for i in all_known if getattr(i, "alive", False))
    stopped_count = len(all_known) - alive_count

    items: list[CardListItem] = []
    for info in all_known:
        role = getattr(info, "role", "") or ""
        age = _format_age(getattr(info, "started_at", 0))
        inbox_count = _count_inbox(spawner, info.name)
        wt = getattr(info, "worktree", None)

        badges: list[CardBadge] = []
        if info.alive:
            badges.append(CardBadge(text="alive", tone="ok"))
        else:
            badges.append(CardBadge(text="stopped", tone="default"))
        if inbox_count:
            badges.append(CardBadge(text=f"📨 {inbox_count}", tone="accent"))
        if wt:
            badges.append(CardBadge(text=f"wt:{wt}", tone="default"))

        subtitle_parts: list[str] = []
        if role:
            subtitle_parts.append(role)
        subtitle_parts.append(f"age {age}")
        subtitle = " · ".join(subtitle_parts) or None

        items.append(CardListItem(
            id=info.name,
            title=info.name,
            subtitle=subtitle,
            icon="agents",
            badges=badges,
            # Clicking the row expands an inline child card showing the
            # teammate's inbox (Phase 3.3). The slash command exists
            # already, so we just point at it.
            expandable_command=f"/agents inbox {info.name}",
            menu=_agents_menu_for(info),
        ))

    summary = f"{alive_count} alive · {stopped_count} stopped" if all_known else None
    card = CardEvent(
        id="agents-roster",
        variant="list",
        title=f"Teammates in {ctx.project_id}",
        icon="agents",
        status="ok",
        payload=CardListPayload(
            items=items,
            empty_hint="no teammates spawned in this project",
            summary=summary,
        ).__dict__,
        actions=[
            CardAction(label="＋ spawn", command="/agents spawn", tone="accent"),
            CardAction(label="↻ refresh", command="/agents", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _spawner_is_alive(spawner, name: str) -> bool:
    """True if a teammate with `name` exists and is alive.

    Prefers list_alive() (stable public API); falls back to the internal
    registry dict for stubs that don't expose list_alive.
    """
    try:
        return any(getattr(t, "name", None) == name
                   for t in spawner.list_alive())
    except Exception:
        pass
    registry = getattr(spawner, "_teammates", {})
    info = registry.get(name)
    return bool(info and getattr(info, "alive", False))


def _agents_menu_for(info) -> list[CardAction]:
    """Build the row-level action menu for a teammate.

    Alive teammates: stop + inbox. Stopped: delete + edit (debug.7.md Task 1d).
    """
    if getattr(info, "alive", False):
        return [
            CardAction(label="stop",
                       command=f"/agents stop {info.name}",
                       tone="default"),
        ]
    return [
        CardAction(label="edit",
                   command=f"/agents edit {info.name}",
                   tone="default"),
        CardAction(label="delete",
                   command=f"/agents delete {info.name}",
                   tone="err"),
    ]


def _parse_edit_args(raw: str) -> tuple[str | None, str | None]:
    """Parse `/agents edit <name> [--role <r>] [--prompt <p>]`.

    Returns (role, prompt); each is None if not provided. Surrounding
    double or single quotes on prompt are stripped.
    """
    raw = raw.strip()
    # Drop leading "edit" token + name.
    if raw.lower().startswith("edit"):
        raw = raw[len("edit"):].strip()
    # Skip the name token (positional).
    tokens = raw.split(None, 1)
    if not tokens:
        return (None, None)
    rest = tokens[1] if len(tokens) > 1 else ""
    role: str | None = None
    prompt: str | None = None
    # Pull --prompt <text> first so its value can contain --role-like text.
    m = re.search(r'(?:^|\s)--prompt[=\s]+(.+?)((?:\s)--role[=\s]+\S+|$)',
                  rest, re.DOTALL)
    if m:
        prompt = m.group(1).strip()
        if (prompt.startswith('"') and prompt.endswith('"')) or \
           (prompt.startswith("'") and prompt.endswith("'")):
            prompt = prompt[1:-1]
        rest = (rest[:m.start()] + rest[m.end():]).strip()
    m = re.search(r'(?:^|\s)--role[=\s]+(\S+)', rest)
    if m:
        role = m.group(1).strip()
    return (role, prompt)


def _parse_spawn_args(raw: str) -> tuple[str, str, str] | str:
    """Parse `/agents spawn <name> <role> --prompt <text>`.

    Returns (name, role, prompt) on success, or an error string.
    Prompt text may be quoted; we honour surrounding double quotes
    because shell-style arg splitting already strips them in real
    command runners, but our tests pass raw strings.
    """
    raw = raw.strip()
    # Drop leading "spawn" token.
    if raw.lower().startswith("spawn"):
        raw = raw[len("spawn"):].strip()
    # Pull --prompt <text> (or --prompt=text) off the tail first so the
    # text can contain spaces without confusing positional parsing.
    prompt = ""
    m = re.search(r'(?:^|\s)--prompt[=\s]+(.+)$', raw, re.DOTALL)
    if m:
        prompt = m.group(1).strip()
        if (prompt.startswith('"') and prompt.endswith('"')) or \
           (prompt.startswith("'") and prompt.endswith("'")):
            prompt = prompt[1:-1]
        raw = raw[:m.start()].strip()
    tokens = raw.split()
    if len(tokens) < 2:
        return ("usage: `/agents spawn <name> <role> "
                "--prompt <text>`")
    if not prompt:
        return ("usage: `/agents spawn <name> <role> "
                "--prompt <text>` (--prompt is required)")
    return tokens[0], tokens[1], prompt


def _peek_inbox(spawner, name: str) -> list[dict] | None:
    """Peek a teammate's inbox without consuming. None if unavailable.

    Narrow the catch to "no such mailbox" / "corrupt JSON" — broader
    catches masked permission errors and IO failures as a silent empty
    inbox, which made production debugging impossible.
    """
    bus = getattr(spawner, "bus", None)
    if bus is None or not hasattr(bus, "peek_inbox"):
        return None
    try:
        return list(bus.peek_inbox(name))
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _count_inbox(spawner, name: str) -> int:
    inbox = _peek_inbox(spawner, name)
    return len(inbox) if inbox is not None else 0


def _format_age(started_at: float) -> str:
    """Human-readable 'started 3m ago' from a monotonic-ish timestamp."""
    if not started_at:
        return "?"
    import time as _time
    delta = max(0, int(_time.time() - started_at))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _cmd_logs(ctx: CommandContext) -> Iterator[dict]:
    """List recent log files written to ``mini_cc/logs/``, as a list card.

    Sorted by mtime desc, capped at 20. Subtitle carries size + mtime
    so the recent-most file is easy to spot.
    """
    from pathlib import Path
    import time as _time
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    if not logs_dir.is_dir():
        yield {"type": "text", "text": f"_logs directory not found: {logs_dir}_"}
        yield {"type": "done"}
        return
    candidates = [p for p in logs_dir.iterdir()
                  if p.is_file() and p.suffix in (".md", ".log", ".txt",
                                                   ".json", ".jsonl")]
    if not candidates:
        yield {"type": "text", "text": f"_no log files in {logs_dir}_"}
        yield {"type": "done"}
        return
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = candidates[:20]
    items: list[CardListItem] = []
    for p in candidates:
        size = p.stat().st_size
        mtime = _time.strftime("%Y-%m-%d %H:%M",
                               _time.localtime(p.stat().st_mtime))
        items.append(CardListItem(
            id=p.name,
            title=p.name,
            subtitle=f"{size:,} bytes · {mtime}",
            icon="logs",
            badges=[],
            menu=[],
        ))
    card = CardEvent(
        id="logs",
        variant="list",
        title="Recent log files",
        icon="logs",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=f"{len(items)} file{'s' if len(items) != 1 else ''}",
            empty_hint=None,
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_loop(ctx: CommandContext) -> Iterator[dict]:
    """Manage scheduled loop jobs for this project.

    With no args: list all cron + wakeup jobs.
    With ``cancel <job_id>``: cancel a cron or wakeup job.
    """
    project = ctx.project
    parts = (ctx.args or "").split()

    cron_jobs: list = []
    wakeups: list = []
    if project is not None:
        if project.scheduler is not None:
            try:
                cron_jobs = list(project.scheduler.list_jobs())
            except Exception:
                cron_jobs = []
        wakeups_mgr = getattr(project, "wakeups", None)
        if wakeups_mgr is not None:
            try:
                wakeups = wakeups_mgr.list()
            except Exception:
                wakeups = []

    if parts and parts[0] == "cancel" and len(parts) >= 2:
        job_id = parts[1]
        if project is None:
            yield {"type": "error", "message": "project context unavailable"}
            return
        # Try cron first, then wakeup.
        if project.scheduler is not None:
            try:
                msg = project.scheduler.cancel(job_id)
                if "cancelled" in msg.lower() or "cancel" in msg.lower():
                    yield {"type": "text", "text": f"🚫 cron: {msg}"}
                    yield {"type": "done"}
                    return
            except Exception:
                pass
        wakeups_mgr = getattr(project, "wakeups", None)
        if wakeups_mgr is not None:
            ok = wakeups_mgr.cancel(job_id)
            if ok:
                yield {"type": "text", "text": f"🚫 cancelled wakeup `{job_id}`"}
                yield {"type": "done"}
                return
        yield {"type": "error",
               "message": f"job {job_id} not found in cron or wakeups"}
        return

    # Default: list everything.
    if not cron_jobs and not wakeups:
        card = CardEvent(
            id="loop",
            variant="list",
            title=f"Scheduled jobs · {ctx.project_id}",
            icon="loop",
            status="ok",
            payload=CardListPayload(
                items=[],
                summary=None,
                empty_hint="no scheduled jobs in this project. Use the agent's schedule_cron or schedule_wakeup tool to create one.",
            ).__dict__,
        )
        yield to_dict(card)
        yield {"type": "done"}
        return
    import time as _time
    now = _time.monotonic()
    items: list[CardListItem] = []
    for j in cron_jobs:
        preview = (j.prompt[:60] + "…") if len(j.prompt) > 60 else j.prompt
        badges: list[CardBadge] = []
        if j.recurring:
            badges.append(CardBadge(text="recurring", tone="accent"))
        else:
            badges.append(CardBadge(text="one-shot", tone="default"))
        if getattr(j, "durable", False):
            badges.append(CardBadge(text="durable", tone="ok"))
        items.append(CardListItem(
            id=j.job_id,
            title=j.job_id,
            subtitle=f"`{j.cron}` · {preview!r}",
            icon="loop",
            badges=badges,
            menu=[CardAction(label="cancel", command=f"/loop cancel {j.job_id}",
                              tone="warn")],
        ))
    for w in sorted(wakeups, key=lambda x: x.fire_at):
        remaining = max(0, int(w.fire_at - now))
        reason_tag = f" · {w.reason}" if w.reason else ""
        preview = (w.prompt[:60] + "…") if len(w.prompt) > 60 else w.prompt
        items.append(CardListItem(
            id=w.wakeup_id,
            title=w.wakeup_id,
            subtitle=f"in {remaining}s{reason_tag} · {preview!r}",
            icon="loop",
            badges=[CardBadge(text="wakeup", tone="default")],
            menu=[CardAction(label="cancel", command=f"/loop cancel {w.wakeup_id}",
                              tone="warn")],
        ))
    summary_bits = []
    if cron_jobs:
        summary_bits.append(f"{len(cron_jobs)} cron")
    if wakeups:
        summary_bits.append(f"{len(wakeups)} wakeup")
    card = CardEvent(
        id="loop",
        variant="list",
        title=f"Scheduled jobs · {ctx.project_id}",
        icon="loop",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=" · ".join(summary_bits),
        ).__dict__,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _cmd_config(ctx: CommandContext) -> Iterator[dict]:
    """Show current effective configuration as a key_value card.

    API keys are flagged ``sensitive`` so the frontend masks them by
    default; a click reveals the value. Useful for debugging "why isn't
    the model connecting" without leaking secrets into chat history.
    """
    cfg = default_config()
    backend = "litellm" if looks_like_litellm(cfg.primary_model) else "anthropic"
    sess_loop = _loop_of(ctx)
    style = getattr(getattr(sess_loop, "state", None), "output_style", None) or "default"

    def kv(k: str, v: str, *, mono: bool = False, sensitive: bool = False) -> CardKeyValuePair:
        return CardKeyValuePair(k=k, v=v, mono=mono, sensitive=sensitive)

    pairs: list[CardKeyValuePair] = [
        kv("primary_model", cfg.primary_model, mono=True),
        kv("fallback_model", cfg.fallback_model or "—", mono=True),
        kv("active backend", backend, mono=True),
        kv("anthropic_base_url", cfg.base_url or "— (default SDK)", mono=True),
        kv("litellm_base_url", cfg.litellm_base_url or "—", mono=True),
        kv("anthropic_api_key", _redact(cfg.api_key), mono=True, sensitive=True),
        kv("litellm_api_key", _redact(cfg.litellm_api_key), mono=True, sensitive=True),
        kv("tavily_api_key", _redact(cfg.tavily_api_key), mono=True, sensitive=True),
        kv("output_style", style, mono=True),
    ]
    card = CardEvent(
        id="config",
        variant="key_value",
        title="Configuration",
        icon="config",
        status="ok",
        payload=CardKeyValuePayload(pairs=pairs).__dict__,
        actions=[
            CardAction(label="↻ reload", command="/config", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _redact(secret: str | None) -> str:
    """Show '—' for None, '••••last4' for short keys, full prefix for long."""
    if not secret:
        return "—"
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


# Valid output-style values. Keep in sync with loop.state.output_style docs.
_OUTPUT_STYLES = ("default", "terse", "detailed", "streamlined")


def _cmd_output_style(ctx: CommandContext) -> Iterator[dict]:
    """Show or set the output-style hint on the active session.

    The hint is stored on ``loop.state.output_style`` and read by the
    system-prompt builder to shape the model's verbosity. With no args
    it shows the current style; with one of the valid values it sets it.
    """
    args = (ctx.args or "").strip()
    sess_loop = _loop_of(ctx)
    if sess_loop is None:
        yield {"type": "error", "message": "session not warm"}
        return
    state = getattr(sess_loop, "state", None)
    if state is None:
        yield {"type": "error", "message": "session has no state"}
        return

    if not args:
        current = getattr(state, "output_style", None) or "default"
        yield {"type": "text",
               "text": (f"**Output style:** `{current}`\n\n"
                        f"Valid values: {', '.join(_OUTPUT_STYLES)}")}
        yield {"type": "done"}
        return
    style = args.split()[0].lower()
    if style not in _OUTPUT_STYLES:
        yield {"type": "error",
               "message": f"unknown style '{style}'. "
                          f"Choose from: {', '.join(_OUTPUT_STYLES)}"}
        return
    state.output_style = style  # type: ignore[attr-defined]
    yield {"type": "text",
           "text": f"📝 output style set to `{style}`. "
                   f"It will shape the next assistant turn."}
    yield {"type": "done"}


def _cmd_workflow(ctx: CommandContext) -> Iterator[dict]:
    """Show the active workflow's status.

    Reads the workflow off the project (set by ``workflow_create``).
    Subcommands:
    - ``clear``       — drop the active workflow
    - ``save``        — persist the active workflow to storage
    - ``load <id>``   — load a saved workflow (by id or unique name) and
                        make it active
    - ``list``        — list saved workflows in this project
    - ``delete <id>`` — remove a saved workflow from storage
    """
    project = ctx.project
    args = (ctx.args or "").strip().split()
    sub = args[0].lower() if args else ""

    if sub == "clear":
        if project is None:
            yield {"type": "error", "message": "project context unavailable"}
            return
        if getattr(project, "active_workflow", None) is None:
            yield {"type": "text", "text": "_no active workflow to clear_"}
        else:
            try:
                object.__setattr__(project, "active_workflow", None)
            except Exception:
                project.active_workflow = None  # type: ignore[attr-defined]
            yield {"type": "text", "text": "🧹 active workflow cleared"}
        yield {"type": "done"}
        return

    if sub == "save":
        yield from _workflow_save(ctx, project)
        return
    if sub == "load":
        yield from _workflow_load(ctx, project, args[1:] if len(args) > 1 else [])
        return
    if sub == "list":
        yield from _workflow_list(ctx, project)
        return
    if sub == "delete":
        yield from _workflow_delete(ctx, project, args[1:] if len(args) > 1 else [])
        return

    wf = getattr(project, "active_workflow", None) if project else None
    if wf is None:
        card = CardEvent(
            id="workflow",
            variant="list",
            title=f"Workflow · {ctx.project_id}",
            icon="workflow",
            status="ok",
            payload=CardListPayload(
                items=[],
                summary=None,
                empty_hint="no active workflow. Use the agent's `workflow_create` tool to start one.",
            ).__dict__,
        )
        yield to_dict(card)
        yield {"type": "done"}
        return

    # Render as a list card: one row per step, status badge tone-coded,
    # with conditional/parallel info as meta. The workflow-level status
    # and progress appear in the summary line.
    def _status_tone(status: str) -> str:
        s = (status or "").lower()
        if s in ("completed", "complete", "done"):
            return "ok"
        if s in ("failed", "aborted", "error"):
            return "err"
        if s in ("paused", "blocked", "pending"):
            return "warn"
        return "accent"  # running

    items: list[CardListItem] = []
    for s in wf.steps:
        done = s.id in wf.results
        is_current = getattr(wf, "current_step", None) == s.id
        badges: list[CardBadge] = []
        if done:
            badges.append(CardBadge(text="done", tone="ok"))
        elif is_current:
            badges.append(CardBadge(text="current", tone="accent"))
        else:
            badges.append(CardBadge(text="pending", tone="default"))
        if s.parallel_with:
            badges.append(CardBadge(text=f"‖ {s.parallel_with}", tone="default"))
        preview = ""
        if s.prompt:
            preview = s.prompt.strip().splitlines()[0][:60]
        meta_bits: list[str] = []
        if s.condition:
            meta_bits.append(f"if {s.condition}")
        items.append(CardListItem(
            id=s.id,
            title=s.id,
            subtitle=preview or None,
            icon="workflow",
            badges=badges,
            meta=" · ".join(meta_bits) if meta_bits else None,
            menu=[],
        ))
    total = len(wf.steps)
    completed = len(wf.results)
    summary = (f"{wf.status} · {completed}/{total} steps"
               f"{' · current: ' + wf.current_step if getattr(wf, 'current_step', None) else ''}")
    card = CardEvent(
        id="workflow",
        variant="list",
        title=f"Workflow · {wf.name}",
        icon="workflow",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=summary,
            empty_hint=None,
        ).__dict__,
        actions=[
            CardAction(label="💾 save", command="/workflow save", tone="default"),
            CardAction(label="🧹 clear", command="/workflow clear", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _workflow_save(ctx: CommandContext, project) -> Iterator[dict]:
    storage = ctx.storage or getattr(project, "storage", None)
    wf = getattr(project, "active_workflow", None) if project else None
    if wf is None:
        yield {"type": "error", "message": "no active workflow to save"}
        return
    if storage is None or not hasattr(storage, "save_workflow"):
        yield {"type": "error", "message": "storage does not support workflows"}
        return
    try:
        payload = wf.to_dict()
        storage.save_workflow(ctx.project_id, payload)
    except Exception as e:
        yield {"type": "error", "message": f"save failed: {e}"}
        return
    yield {"type": "text",
           "text": f"💾 workflow `{wf.name}` (`{wf.id}`) saved"}
    yield {"type": "done"}


def _workflow_load(ctx: CommandContext, project, rest: list[str]) -> Iterator[dict]:
    storage = ctx.storage or getattr(project, "storage", None)
    if not rest:
        yield {"type": "error", "message": "usage: /workflow load <id|name>"}
        return
    if storage is None or not hasattr(storage, "load_workflow"):
        yield {"type": "error", "message": "storage does not support workflows"}
        return
    key = rest[0]
    saved = storage.load_workflow(ctx.project_id, key)
    if saved is None:
        # Look it up by name across all saved workflows.
        for w in storage.list_workflows(ctx.project_id):
            if w.get("name") == key:
                saved = w
                break
    if saved is None:
        yield {"type": "error", "message": f"no saved workflow matching `{key}`"}
        return
    from ..workflow import workflow_from_dict
    wf = workflow_from_dict(saved)
    wf.results = dict(saved.get("results") or {})
    wf.state = dict(saved.get("state") or {})
    wf.status = saved.get("status") or wf.status
    try:
        object.__setattr__(project, "active_workflow", wf)
    except Exception:
        project.active_workflow = wf  # type: ignore[attr-defined]
    yield {"type": "text",
           "text": f"📂 loaded workflow `{wf.name}` (`{wf.id}`) — "
                   f"{len(wf.steps)} steps, {len(wf.results)} completed"}
    yield {"type": "done"}


def _workflow_list(ctx: CommandContext, project) -> Iterator[dict]:
    storage = ctx.storage or getattr(project, "storage", None)
    if storage is None or not hasattr(storage, "list_workflows"):
        yield {"type": "error", "message": "storage does not support workflows"}
        return
    rows = storage.list_workflows(ctx.project_id)
    if not rows:
        yield {"type": "text", "text": "_no saved workflows in this project_"}
        yield {"type": "done"}
        return
    lines = [f"**Saved workflows in `{ctx.project_id}`:**", ""]
    for w in rows:
        wid = w.get("id", "?")
        name = w.get("name", "?")
        steps = len(w.get("steps") or [])
        done = len(w.get("results") or {})
        status = w.get("status", "?")
        saved_at = (w.get("saved_at") or "")[:19]
        lines.append(f"- `{wid}` — **{name}** · {done}/{steps} done · "
                     f"`{status}` · {saved_at}")
    lines.append("")
    lines.append("Load with `/workflow load <id>`")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _workflow_delete(ctx: CommandContext, project, rest: list[str]) -> Iterator[dict]:
    storage = ctx.storage or getattr(project, "storage", None)
    if not rest:
        yield {"type": "error", "message": "usage: /workflow delete <id>"}
        return
    if storage is None or not hasattr(storage, "delete_workflow"):
        yield {"type": "error", "message": "storage does not support workflows"}
        return
    wf_id = rest[0]
    if storage.delete_workflow(ctx.project_id, wf_id):
        yield {"type": "text", "text": f"🗑️ deleted workflow `{wf_id}`"}
    else:
        yield {"type": "error", "message": f"no saved workflow `{wf_id}`"}
    yield {"type": "done"}


def _cmd_bg(ctx: CommandContext) -> Iterator[dict]:
    """List background tasks spawned via bash run_in_background or the
    loop's slow-op offload. ``/bg stop <bg_id>`` cancels one."""
    project = ctx.project
    bg = getattr(project, "background", None) if project else None
    if bg is None:
        yield {"type": "text",
               "text": "_background scheduler not configured for this project_"}
        yield {"type": "done"}
        return

    parts = (ctx.args or "").split()
    if parts and parts[0].lower() == "stop" and len(parts) >= 2:
        bg_id = parts[1]
        try:
            msg = bg.stop(bg_id)
        except Exception as e:
            yield {"type": "error", "message": f"stop failed: {e}"}
            return
        yield {"type": "text", "text": f"🛑 {msg}"}
        yield {"type": "done"}
        return

    tasks = bg.list_tasks()
    counts: dict[str, int] = {}
    items: list[CardListItem] = []
    for t in tasks:
        status = t.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        bg_id = t.get("bg_id", "?")
        cmd = (t.get("command") or "")
        cmd_display = (cmd[:60] + "…") if len(cmd) > 60 else cmd
        tone = {"running": "ok", "completed": "default",
                "stopped": "warn"}.get(status, "default")
        badge = CardBadge(text=status, tone=tone)
        items.append(CardListItem(
            id=bg_id,
            title=bg_id,
            subtitle=cmd_display or None,
            icon="bg",
            badges=[badge],
            menu=[CardAction(label="stop", command=f"/bg stop {bg_id}",
                              tone="warn")] if status == "running" else [],
        ))
    summary_bits = [f"{v} {k}" for k, v in counts.items() if v]
    card = CardEvent(
        id="bg",
        variant="list",
        title=f"Background tasks · {ctx.project_id}",
        icon="bg",
        status="ok",
        payload=CardListPayload(
            items=items,
            summary=" · ".join(summary_bits) if items else None,
            empty_hint="no background tasks in this project. Use `bash run_in_background=true` to start one." if not items else None,
        ).__dict__,
        # Live-refresh every 3s while any task is still running. The
        # frontend CardView polls /bg and replaces this card by id, so
        # the user sees status transitions without re-typing the
        # command. Stopped-only rosters are static.
        refresh_command="/bg" if counts.get("running") else None,
        refresh_interval_ms=3000 if counts.get("running") else None,
    )
    yield to_dict(card)
    yield {"type": "done"}


def _loop_of(ctx: CommandContext):
    sm = ctx.session_manager
    if sm is None:
        return None
    sess = sm._sessions.get((ctx.project_id, ctx.session_id))
    if sess is None:
        return None
    return getattr(sess, "loop", None)


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


def default_registry() -> CommandRegistry:
    """Return the process-wide registry, populating built-ins on first call."""
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    reg = CommandRegistry()
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
        name="compact",
        description="Manually trigger history compaction.",
        handler=_cmd_compact,
    ))
    reg.register(SlashCommand(
        name="skills",
        description="List skills available in this project.",
        handler=_cmd_skills,
    ))
    reg.register(SlashCommand(
        name="mcp",
        description="List MCP servers (connected + available to connect).",
        handler=_cmd_mcp,
    ))
    reg.register(SlashCommand(
        name="tasks",
        description="List durable tasks in this project.",
        handler=_cmd_tasks,
    ))
    reg.register(SlashCommand(
        name="cost",
        description="Show token usage and request counts for this tenant.",
        handler=_cmd_cost,
    ))
    reg.register(SlashCommand(
        name="permissions",
        description="Show the sandbox policy (blocked commands, allowed git, env).",
        handler=_cmd_permissions,
    ))
    reg.register(SlashCommand(
        name="agents",
        description="List teammates spawned in this project (alive + recently stopped).",
        handler=_cmd_agents,
    ))
    reg.register(SlashCommand(
        name="logs",
        description="List recent log files under mini_cc/logs/.",
        handler=_cmd_logs,
    ))
    reg.register(SlashCommand(
        name="loop",
        description="List scheduled cron + wakeup jobs. `/loop cancel <id>` removes one.",
        handler=_cmd_loop,
    ))
    reg.register(SlashCommand(
        name="config",
        description="Show effective configuration (model, providers, API key status).",
        handler=_cmd_config,
    ))
    reg.register(SlashCommand(
        name="output-style",
        description="Show or set the output-style hint (terse / default / detailed / streamlined).",
        handler=_cmd_output_style,
    ))
    reg.register(SlashCommand(
        name="workflow",
        description="Show the active workflow's status. `/workflow clear` drops it.",
        handler=_cmd_workflow,
    ))
    reg.register(SlashCommand(
        name="bg",
        description="List background tasks. `/bg stop <bg_id>` cancels one.",
        handler=_cmd_bg,
    ))
    reg.register(SlashCommand(
        name="resume",
        description="List recent sessions, or resume one by id: `/resume <session_id>`.",
        handler=_cmd_resume,
    ))
    reg.register(SlashCommand(
        name="tools",
        description="List all tools (builtin + MCP) the agent can call.",
        handler=_cmd_tools,
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
