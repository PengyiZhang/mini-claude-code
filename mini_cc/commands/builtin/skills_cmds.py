"""Skills / tools / MCP slash commands (split from commands/registry.py — M3-1)."""
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
from ..registry import CommandContext, SlashCommand

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
    from ...tools import builtin_tools
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
    # debug.8 Task B: servers known from a stored spec but currently
    # disconnected — surface them in a "disconnected" state so the user
    # can reconnect without a server restart.
    try:
        known = list(pool.list_known_servers()) if hasattr(pool, "list_known_servers") else []
    except Exception:
        known = []
    connectable = sorted(s for s in available if s not in connected)
    try:
        attempts = pool.list_attempts()
    except Exception:
        attempts = {}
    failed = sorted(
        name for name, rec in attempts.items()
        if not getattr(rec, "ok", False)
        and name not in connected
        and name not in known)
    disconnected = sorted(
        s for s in known if s not in connected)
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
    for name in disconnected:
        # Spec retained, just not currently connected. Show transport
        # type as the subtitle so the user knows what they're reconnecting.
        spec = pool.get_spec(name) if hasattr(pool, "get_spec") else None
        sub = f"{(spec or {}).get('type', 'mcp')} — click reconnect"
        items.append(CardListItem(
            id=f"mcp:{name}",
            title=name,
            subtitle=sub,
            icon="mcp",
            badges=[CardBadge(text="disconnected", tone="warn")],
            menu=[
                CardAction(label="reconnect",
                           command=f"/mcp reconnect {name}", tone="accent"),
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
    if disconnected:
        summary_bits.append(f"{len(disconnected)} disconnected")
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
    # reconnect (debug.8 Task B): prefer pool.reconnect() which knows
    # about stored specs; fall back to disconnect+connect for older pool
    # implementations without the new method.
    reconnect_fn = getattr(pool, "reconnect", None)
    if callable(reconnect_fn):
        try:
            ok, msg = reconnect_fn(name)
        except Exception as e:
            yield {"type": "error",
                   "message": f"reconnect failed: {e}"}
            yield {"type": "done"}
            return
    else:
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



def register(reg) -> None:

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
        name="tools",
        description="List all tools (builtin + MCP) the agent can call.",
        handler=_cmd_tools,
    ))