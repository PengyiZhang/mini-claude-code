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
        if sub == "stop" and len(parts) >= 2:
            name = parts[1]
            try:
                msg = spawner.request_shutdown(name)
            except Exception as e:
                yield {"type": "error", "message": f"shutdown failed: {e}"}
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
                return
            if not inbox:
                yield {"type": "text",
                       "text": f"_`{name}` has an empty inbox._"}
                yield {"type": "done"}
                return
            lines = [f"**Inbox for `{name}` ({len(inbox)}):**", ""]
            for i, m in enumerate(inbox[:10], 1):
                sender = m.get("from_agent", "?")
                kind = m.get("kind", "message")
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
                          "Use `/agents`, `/agents stop <name>`, "
                          "or `/agents inbox <name>`."}
        return

    # Default: list everyone.
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


def _peek_inbox(spawner, name: str) -> list[dict] | None:
    """Peek a teammate's inbox without consuming. None if unavailable."""
    bus = getattr(spawner, "bus", None)
    if bus is None or not hasattr(bus, "peek_inbox"):
        return None
    try:
        return list(bus.peek_inbox(name))
    except Exception:
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
    With ``clear`` arg, drops the active workflow so a new one can be
    created.
    """
    project = ctx.project
    args = (ctx.args or "").strip().split()
    if args and args[0].lower() == "clear":
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
    lines.append("Subcommands: `/workflow clear`")
    yield {"type": "text", "text": "\n".join(lines)}
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
    _DEFAULT = reg
    return reg
