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
    """Yield a markdown table of available commands as a single text event."""
    reg = default_registry()
    lines = ["**Available commands:**", ""]
    for c in reg.all_visible():
        aliases = f" (aliases: {', '.join('/' + a for a in c.aliases)})"
        lines.append(f"- `/{c.name}` — {c.description}{aliases if c.aliases else ''}")
    lines.append("")
    lines.append("Tip: type `/` in the input box to see the menu.")
    yield {"type": "text", "text": "\n".join(lines)}
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
    """List sessions in this project, marking the active one."""
    sm = ctx.session_manager
    if sm is None or ctx.project is None:
        yield {"type": "error", "message": "project context unavailable"}
        return
    metas = sm.list(ctx.project_id)
    if not metas:
        yield {"type": "text", "text": "_no sessions in this project_"}
        yield {"type": "done"}
        return
    lines = [f"**Sessions in `{ctx.project_id}`:**", ""]
    for m in metas:
        marker = " ← active" if m.session_id == ctx.session_id else ""
        warm = "🟢" if m.in_memory else "⚪"
        lines.append(f"- {warm} `{m.session_id}`{marker}")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_model(ctx: CommandContext) -> Iterator[dict]:
    """Report the current model + which provider backend will handle it."""
    cfg = default_config()
    model = cfg.primary_model
    backend = "litellm" if looks_like_litellm(model) else "anthropic"
    fallback = cfg.fallback_model or "—"
    yield {"type": "text", "text": (
        f"**Current model**\n"
        f"- model: `{model}`\n"
        f"- backend: `{backend}`\n"
        f"- fallback: `{fallback}`\n"
        f"- session model override: "
        f"`{getattr(getattr(_loop_of(ctx), 'state', None), 'current_model', '—')}`"
    )}
    yield {"type": "done"}


def _cmd_skills(ctx: CommandContext) -> Iterator[dict]:
    """List skills available in this project.

    Skills are discovered from <workspace>/skills/*/SKILL.md by the
    project's SkillLoader. The model loads one on demand via the
    ``load_skill`` tool; this command just shows the catalog.
    """
    project = ctx.project
    if project is None or project.skills_loader is None:
        yield {"type": "error", "message": "skills not configured for this project"}
        return
    # Re-scan so newly added skills appear without a server restart.
    try:
        project.skills_loader.scan()
    except Exception:
        pass
    reg = project.skills_loader.registry
    if not reg:
        yield {"type": "text",
               "text": "_no skills found in this project. Drop a skill into "
                       "`<workspace>/skills/<name>/SKILL.md` and run `/skills` again._"}
        yield {"type": "done"}
        return
    lines = [f"**Skills in `{ctx.project_id}`:**", ""]
    for s in reg.values():
        desc = (s.description or "").strip().splitlines()[0] if s.description else ""
        lines.append(f"- `{s.name}` — {desc}" if desc else f"- `{s.name}`")
    lines.append("")
    lines.append("Tip: ask the agent to `load_skill <name>` to use one.")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_tools(ctx: CommandContext) -> Iterator[dict]:
    """List every tool the agent can call this turn.

    Combines the builtin tool registry (bash, fs, web_search, ...) with
    the live MCP tools from any connected MCP server. The same view the
    Anthropic API sees in the ``tools`` field — useful for debugging
    "why doesn't the agent see my tool" (e.g. web_search requires
    TAVILY_API_KEY to *succeed* but is always *registered*).
    """
    from ..tools import builtin_tools
    project = ctx.project
    lines = [f"**Tools visible to the agent in `{ctx.project_id}`:**", ""]
    builtin = [t.name for t in builtin_tools()]
    lines.append(f"**builtin ({len(builtin)}):**")
    for name in sorted(builtin):
        lines.append(f"- `{name}`")
    mcp_names: list[str] = []
    if project is not None and project.mcp_pool is not None:
        try:
            for t in project.mcp_pool.all_tools():
                mcp_names.append(t.name)
        except Exception:
            pass
    lines.append("")
    lines.append(f"**MCP ({len(mcp_names)}):**")
    if mcp_names:
        for name in sorted(mcp_names):
            lines.append(f"- `{name}`")
    else:
        lines.append("_none — no MCP servers connected_")
    lines.append("")
    lines.append("Note: tools like `web_search` are always listed but return "
                 "an error at runtime if their API key is missing.")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_search(ctx: CommandContext) -> Iterator[dict]:
    """F3.1: substring search across every session in the project."""
    query = (ctx.args or "").strip()
    if not query:
        yield {"type": "text",
               "text": "Usage: `/search <query>` — searches every session's "
                       "messages in this project."}
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
        hits = storage.search_messages(ctx.project_id, query, limit=20)
    except Exception as e:
        yield {"type": "text", "text": f"Error: {type(e).__name__}: {e}"}
        yield {"type": "done"}
        return
    if not hits:
        yield {"type": "text", "text": f"No matches for `{query}`."}
        yield {"type": "done"}
        return
    lines = [f"**Found {len(hits)} match{'es' if len(hits)!=1 else ''} "
             f"for `{query}`:**", ""]
    for h in hits:
        lines.append(f"- **{h.session_id}** ({h.role}, msg #{h.message_index + 1}):")
        lines.append(f"  > {h.snippet}")
    lines.append("")
    lines.append("Use `/resume <session_id>` to open a matching session.")
    yield {"type": "text", "text": "\n".join(lines)}
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
    """List MCP servers registered for this project.

    Reports both the servers the project is currently connected to
    (their tools are live in the loop's tool pool) and the servers
    that could be connected (registered factories not yet attached).
    """
    project = ctx.project
    if project is None or project.mcp_pool is None:
        yield {"type": "error", "message": "MCP not configured for this project"}
        return
    pool = project.mcp_pool
    connected = list(pool.list_connected())
    try:
        available = list(type(pool).available_servers())
    except Exception:
        available = []
    connectable = sorted(s for s in available if s not in connected)
    # Discovered-on-disk servers that were tried at assembly time but
    # failed to connect (auth error, unreachable host, …). Without
    # surfacing these the user sees "no servers registered" and has no
    # clue why their .mcp.json didn't take effect.
    try:
        attempts = pool.list_attempts()
    except Exception:
        attempts = {}
    failed = sorted(
        name for name, rec in attempts.items()
        if not rec.ok and name not in connected)
    if not connected and not connectable and not failed:
        yield {"type": "text",
               "text": "_no MCP servers registered. Drop a ``.mcp.json`` "
                       "into any ``.mini_cc/`` tier, or register a factory "
                       "at app startup via "
                       "``MCPPool.register_factory(name, fn)``._"}
        yield {"type": "done"}
        return
    lines = [f"**MCP servers for `{ctx.project_id}`:**", ""]
    if connected:
        lines.append("**connected:**")
        for name in connected:
            client = pool._clients.get(name)
            tool_count = len(getattr(client, "tools", []) or [])
            lines.append(f"- 🟢 `{name}` ({tool_count} tools live)")
    if failed:
        if connected:
            lines.append("")
        lines.append("**failed to connect (discovered in .mcp.json/mcp.toml):**")
        for name in failed:
            reason = attempts[name].message
            lines.append(f"- 🔴 `{name}` — {reason}")
            lines.append(f"  _edit the matching entry in `.mini_cc/.mcp.json` "
                         f"(or `mcp.toml`) and re-open the session to retry._")
    if connectable:
        if connected or failed:
            lines.append("")
        lines.append("**available (not connected):**")
        for name in connectable:
            lines.append(f"- ⚪ `{name}` — ask the agent to "
                         f"`connect_mcp {name}`")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_tasks(ctx: CommandContext) -> Iterator[dict]:
    """List durable tasks created via create_task."""
    if ctx.project is None or ctx.storage is None:
        yield {"type": "error", "message": "project context unavailable"}
        return
    tasks = ctx.storage.load_tasks(ctx.project_id)
    if not tasks:
        yield {"type": "text", "text": "_no tasks in this project_"}
        yield {"type": "done"}
        return
    lines = [f"**Tasks in `{ctx.project_id}`:**", ""]
    for t in tasks:
        owner = f" (owner:{t.owner})" if t.owner else ""
        wt = f" (wt:{t.worktree})" if t.worktree else ""
        lines.append(f"- `{t.id}` [{t.status}] {t.subject}{owner}{wt}")
    yield {"type": "text", "text": "\n".join(lines)}
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
    """Show token usage for the current tenant across the project.

    Pulls from MetricsRegistry (attached to Project.metrics). When no
    registry is wired in we surface a friendly "—" rather than 0 so
    users can tell "no usage yet" from "metrics disabled".
    """
    project = ctx.project
    if project is None or project.metrics is None:
        yield {"type": "text", "text": "_metrics not configured for this project_"}
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

    lines = [f"**Cost snapshot** (`{tenant}`)", "",
             f"- input tokens: **{inp:,}**",
             f"- output tokens: **{out:,}**",
             f"- cache read: `{cache_read:,}`",
             f"- cache create: `{cache_create:,}`",
             f"- **total: {total:,}**",
             ""]
    if by_status:
        outcome_bits = [f"{k}={v}" for k, v in sorted(by_status.items())]
        lines.append(f"- requests: {', '.join(outcome_bits)}")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_permissions(ctx: CommandContext) -> Iterator[dict]:
    """Show the sandbox policy: blocked command patterns, allowed git
    subcommands, and the env var whitelist. Useful for understanding
    why a command was denied without digging into source files."""
    project = ctx.project
    sandbox = getattr(project, "sandbox", None) if project else None
    if sandbox is None or not hasattr(sandbox, "policy"):
        yield {"type": "text",
               "text": "_sandbox policy not available for this project_"}
        yield {"type": "done"}
        return
    policy = sandbox.policy
    lines = [f"**Sandbox policy** (`{ctx.project_id}`)", ""]

    lines.append("**Blocked command patterns:**")
    if policy.blocked:
        for name, rx in policy.blocked:
            lines.append(f"- `{name}` — `{rx}`")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append("**Allowed git subcommands:**")
    lines.append(", ".join(f"`{s}`" for s in sorted(policy.allowed_git)))
    lines.append("")

    lines.append("**Forwarded env vars:**")
    lines.append(", ".join(f"`{v}`" for v in sorted(policy.allowed_env)))

    # Surface the hook-level permission gate too (DENY_LIST / DESTRUCTIVE)
    # if the project has one wired.
    from ..core.hooks import DENY_LIST, DESTRUCTIVE
    lines.append("")
    lines.append("**Permission hook deny-lists:**")
    lines.append(f"- DENY_LIST: {', '.join(repr(s) for s in DENY_LIST)}")
    lines.append(f"- DESTRUCTIVE: {', '.join(repr(s) for s in DESTRUCTIVE)}")

    yield {"type": "text", "text": "\n".join(lines)}
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
        yield {"type": "error",
               "message": f"unknown subcommand '{sub}'. "
                          "Use `/agents`, `/agents spawn ...`, "
                          "`/agents stop <name>`, or `/agents inbox <name>`."}
        yield {"type": "done"}
        return
    all_known = list(getattr(spawner, "_teammates", {}).values())
    if not all_known:
        yield {"type": "text", "text": "_no teammates spawned in this project_"}
        yield {"type": "done"}
        return
    lines = [f"**Teammates in `{ctx.project_id}`:**", ""]
    alive_count = 0
    for info in all_known:
        role = getattr(info, "role", "") or ""
        marker = "🟢" if getattr(info, "alive", False) else "⚫"
        if info.alive:
            alive_count += 1
        age = _format_age(getattr(info, "started_at", 0))
        inbox_count = _count_inbox(spawner, info.name)
        wt = getattr(info, "worktree", None)
        wt_tag = f" · wt:`{wt}`" if wt else ""
        inbox_tag = f" · 📨{inbox_count}" if inbox_count else ""
        lines.append(f"- {marker} `{info.name}` — {role} "
                     f"(age {age}{wt_tag}{inbox_tag})")
    lines.append("")
    lines.append(f"_{alive_count} alive · {len(all_known) - alive_count} "
                 f"stopped_")
    lines.append("")
    lines.append("Subcommands: `/agents stop <name>`, "
                 "`/agents inbox <name>`")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


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
    """List recent log files written to ``mini_cc/logs/``.

    The log directory is a sibling of the package, so resolve it
    relative to the package root rather than the project workspace.
    Each file's mtime determines recency.
    """
    from pathlib import Path
    import os
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
    # Sort by mtime desc; cap at 20 so the listing stays useful.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = candidates[:20]
    lines = [f"**Recent log files** (`{logs_dir}`)", ""]
    import time as _time
    for p in candidates:
        size = p.stat().st_size
        mtime = _time.strftime("%Y-%m-%d %H:%M",
                               _time.localtime(p.stat().st_mtime))
        lines.append(f"- `{p.name}` — {size:,} bytes — {mtime}")
    yield {"type": "text", "text": "\n".join(lines)}
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
        if project.wakeups is not None:
            try:
                wakeups = project.wakeups.list()
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
        if project.wakeups is not None:
            ok = project.wakeups.cancel(job_id)
            if ok:
                yield {"type": "text", "text": f"🚫 cancelled wakeup `{job_id}`"}
                yield {"type": "done"}
                return
        yield {"type": "error",
               "message": f"job {job_id} not found in cron or wakeups"}
        return

    # Default: list everything.
    if not cron_jobs and not wakeups:
        yield {"type": "text",
               "text": "_no scheduled jobs in this project. "
                       "Use the agent's schedule_cron or schedule_wakeup "
                       "tool to create one._"}
        yield {"type": "done"}
        return
    import time as _time
    lines = [f"**Scheduled jobs in `{ctx.project_id}`:**", ""]
    if cron_jobs:
        lines.append(f"**Cron jobs ({len(cron_jobs)}):**")
        for j in cron_jobs:
            kind = "🔁 recurring" if j.recurring else "⚡ one-shot"
            durable = " · 💾 durable" if j.durable else ""
            preview = (j.prompt[:60] + "…") if len(j.prompt) > 60 else j.prompt
            lines.append(f"- {kind}`{j.job_id}` `{j.cron}`{durable}")
            lines.append(f"  prompt: {preview!r}")
        lines.append("")
    if wakeups:
        lines.append(f"**Pending wakeups ({len(wakeups)}):**")
        now = _time.monotonic()
        for w in sorted(wakeups, key=lambda x: x.fire_at):
            remaining = max(0, int(w.fire_at - now))
            reason_tag = f" — {w.reason}" if w.reason else ""
            preview = (w.prompt[:60] + "…") if len(w.prompt) > 60 else w.prompt
            lines.append(f"- ⏰ `{w.wakeup_id}` in {remaining}s{reason_tag}")
            lines.append(f"  prompt: {preview!r}")
    yield {"type": "text", "text": "\n".join(lines)}
    yield {"type": "done"}


def _cmd_config(ctx: CommandContext) -> Iterator[dict]:
    """Show current effective configuration.

    Redacts API keys (shows only the last 4 chars + prefix). Useful
    for debugging "why isn't the model connecting" without leaking
    secrets into chat history.
    """
    cfg = default_config()
    lines = ["**Effective configuration:**", ""]
    lines.append(f"- primary_model: `{cfg.primary_model}`")
    lines.append(f"- fallback_model: `{cfg.fallback_model or '—'}`")
    lines.append(f"- anthropic_api_key: `{_redact(cfg.api_key)}`")
    lines.append(f"- anthropic_base_url: `{cfg.base_url or '— (default SDK)'}`")
    lines.append(f"- litellm_api_key: `{_redact(cfg.litellm_api_key)}`")
    lines.append(f"- litellm_base_url: `{cfg.litellm_base_url or '—'}`")
    lines.append(f"- tavily_api_key: `{_redact(cfg.tavily_api_key)}`")
    # Provider routing hint.
    backend = "litellm" if looks_like_litellm(cfg.primary_model) else "anthropic"
    lines.append(f"- active backend: `{backend}`")
    # Output style if set on the session.
    sess_loop = _loop_of(ctx)
    style = getattr(getattr(sess_loop, "state", None), "output_style", None)
    lines.append(f"- output_style: `{style or 'default'}`")
    yield {"type": "text", "text": "\n".join(lines)}
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
        yield {"type": "text",
               "text": "_no active workflow. Use the agent's "
                       "`workflow_create` tool to start one._"}
        yield {"type": "done"}
        return

    # Render steps + state + results.
    from ..workflow import Workflow  # local to avoid import cycles
    lines = [f"**Workflow:** `{wf.name}` (`{wf.id}`)", ""]
    if wf.description:
        lines.append(f"_{wf.description}_")
        lines.append("")
    lines.append(f"- status: **{wf.status}**")
    lines.append(f"- steps: {len(wf.steps)} · completed: {len(wf.results)}")
    if wf.state:
        state_keys = ", ".join(f"`{k}`" for k in sorted(wf.state.keys()))
        lines.append(f"- state: {state_keys}")
    if wf.steps:
        lines.append("")
        lines.append("**Steps:**")
        for s in wf.steps:
            done = s.id in wf.results
            tag = "✅" if done else "◻️"
            cond = f" _if `{s.condition}`_" if s.condition else ""
            par = (f" _parallel with `{s.parallel_with}`)_"
                   if s.parallel_with else "")
            preview = s.prompt.strip().splitlines()[0][:60] if s.prompt else ""
            lines.append(f"- {tag} `{s.id}`{cond}{par}")
            if preview:
                lines.append(f"    _{preview}_")
    lines.append("")
    lines.append("Subcommands: `/workflow save|load <id>|list|delete <id>|clear`")
    yield {"type": "text", "text": "\n".join(lines)}
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
    if not tasks:
        yield {"type": "text",
               "text": "_no background tasks in this project. "
                       "Use `bash run_in_background=true` to start one._"}
        yield {"type": "done"}
        return
    lines = [f"**Background tasks in `{ctx.project_id}`:**", ""]
    counts = {"running": 0, "completed": 0, "stopped": 0}
    for t in tasks:
        status = t.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        marker = {"running": "🟢", "completed": "✅",
                  "stopped": "⛔"}.get(status, "❓")
        cmd = (t.get("command") or "")[:60]
        cmd_display = cmd + ("…" if len((t.get("command") or "")) > 60 else "")
        lines.append(f"- {marker} `{t.get('bg_id')}` — {cmd_display}")
    summary_bits = [f"{v} {k}" for k, v in counts.items() if v]
    lines.append("")
    lines.append(f"_{' · '.join(summary_bits)}_")
    lines.append("")
    lines.append("Subcommands: `/bg stop <bg_id>`")
    yield {"type": "text", "text": "\n".join(lines)}
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
