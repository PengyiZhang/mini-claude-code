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
    if not connected and not connectable:
        yield {"type": "text",
               "text": "_no MCP servers registered. Register a factory at app "
                       "startup via `MCPPool.register_factory(name, fn)`._"}
        yield {"type": "done"}
        return
    lines = [f"**MCP servers for `{ctx.project_id}`:**", ""]
    if connected:
        lines.append("**connected:**")
        for name in connected:
            client = pool._clients.get(name)
            tool_count = len(getattr(client, "tools", []) or [])
            lines.append(f"- 🟢 `{name}` ({tool_count} tools live)")
    if connectable:
        if connected:
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


def _loop_of(ctx: CommandContext):
    sm = ctx.session_manager
    if sm is None:
        return None
    sess = sm._sessions.get((ctx.project_id, ctx.session_id))
    if sess is None:
        return None
    return getattr(sess, "loop", None)


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
    _DEFAULT = reg
    return reg
