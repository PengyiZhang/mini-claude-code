"""Teammates (/agents) slash commands and helpers (split from commands/registry.py — M3-1)."""
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
            # Sub-verbs: `ack <ts>` marks read; `ignore <ts>` marks ignored.
            # Otherwise default to listing the inbox (no 10-cap since
            # peek_inbox is O(1) post-cache-upgrade).
            verb = parts[2] if len(parts) >= 3 else None
            disp = getattr(spawner, "disposition", None)
            if verb in ("ack", "ignore") and len(parts) >= 4:
                if disp is None:
                    yield {"type": "error",
                           "message": "disposition tracking unavailable"}
                    yield {"type": "done"}
                    return
                try:
                    ts = float(parts[3])
                except ValueError:
                    yield {"type": "error",
                           "message": f"invalid ts: {parts[3]!r}"}
                    yield {"type": "done"}
                    return
                if verb == "ack":
                    disp.mark_read(name, ts)
                else:
                    disp.mark_ignored(name, ts)
                yield {"type": "text",
                       "text": f"marked `{name}` ts={ts} as {verb}"}
                yield {"type": "done"}
                return
            inbox = _inbox_view(spawner, name)
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
            # Build a CardEvent with one item per message so the
            # TeammatesPanel can render disposition badges + actions
            # without re-parsing markdown.
            states = disp.dispositions_for(name) if disp else {}
            # Derive the disposition rather than trusting only the sidecar:
            # a message still in the LIVE inbox (peek, not yet drained) is
            # "unread"; once the teammate has drained it (only in history)
            # it is "read". The sidecar's explicit "ignored" wins, and a
            # manual "ack" (sidecar "read") marks a still-queued message
            # read too. Pre-fix every drained message showed "unread" until
            # the lead manually acked — the opposite of what "read" should
            # mean, since the teammate already saw it.
            live = _peek_inbox(spawner, name) or []
            live_ts = {m.get("ts") for m in live if isinstance(m, dict)}
            items: list[dict] = []
            for i, m in enumerate(inbox, 1):
                sender = m.get("from") or m.get("from_agent") or "?"
                kind = m.get("type") or m.get("kind") or "message"
                ts = m.get("ts")
                body = (m.get("content") or "").strip()
                if len(body) > 80:
                    body = body[:80] + "…"
                sidecar = states.get(str(ts)) if ts else None
                if sidecar == "ignored":
                    state = "ignored"
                elif sidecar == "read" or ts not in live_ts:
                    state = "read"
                else:
                    state = "unread"
                tone = {"read": "ok",
                        "ignored": "muted"}.get(state, "warn")
                # Embed ack/ignore actions so the CardShell menu can
                # trigger them without the frontend needing to know
                # the ts separately. ts is encoded in the command string.
                ts_str = f"{ts}" if ts else ""
                menu = []
                if state != "read" and ts:
                    menu.append({
                        "label": "ack",
                        "command": f"/agents inbox {name} ack {ts_str}",
                        "tone": "ok",
                    })
                if state != "ignored" and ts:
                    menu.append({
                        "label": "ignore",
                        "command": f"/agents inbox {name} ignore {ts_str}",
                        "tone": "muted",
                    })
                items.append({
                    "id": f"{name}-inbox-{i}",
                    "title": f"@{sender} · {kind}",
                    "subtitle": body,
                    "meta": _fmt_ts(ts) if ts else "",
                    "badges": [{"text": state, "tone": tone}],
                    "menu": menu,
                })
            inbox_card = CardEvent(
                id=f"agents-inbox-{name}",
                variant="list",
                title=f"Inbox · @{name} ({len(inbox)})",
                icon="agents",
                status="ok",
                payload=CardListPayload(
                    items=items,
                    empty_hint=f"@{name} has no messages",
                    summary=(f"{len(inbox)} message"
                             if len(inbox) == 1
                             else f"{len(inbox)} messages"),
                ).__dict__,
                actions=[
                    CardAction(label="↻ refresh",
                               command=f"/agents inbox {name}",
                               tone="default"),
                ],
            )
            yield to_dict(inbox_card)
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


def _inbox_view(spawner, name: str) -> list[dict] | None:
    """Return the user-facing inbox for ``name``: history + any messages
    that arrived after the last history write (still in the live cache).

    History is the source of truth — the live inbox is drained every
    few seconds by TeammateSpawner._idle_poll, so peek_inbox alone
    returns an empty list moments after delivery. We union live with
    history, dedupe by ts, and return most-recent-last so the Card list
    reads top-down chronologically.

    Returns None if no bus is wired (so the caller can emit an error).
    """
    bus = getattr(spawner, "bus", None)
    if bus is None:
        return None
    history_fn = getattr(bus, "history", None)
    if not callable(history_fn):
        # Bus without history support — fall back to peek-only.
        return _peek_inbox(spawner, name)
    try:
        hist = list(history_fn(name))
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        hist = []
    try:
        live = list(bus.peek_inbox(name))
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        live = []
    if not live:
        return hist
    if not hist:
        return live
    # Merge: append live entries not already in history (by ts).
    seen = {m.get("ts") for m in hist if isinstance(m, dict)}
    for m in live:
        if isinstance(m, dict) and m.get("ts") not in seen:
            hist.append(m)
    return hist


def _count_inbox(spawner, name: str) -> int:
    inbox = _peek_inbox(spawner, name)
    return len(inbox) if inbox is not None else 0


def _fmt_ts(ts: float | None) -> str:
    """Render an inbox message timestamp as a short relative-age string
    for the TeammatesPanel history view. Returns '' for missing ts."""
    if ts is None:
        return ""
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


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



def register(reg) -> None:

    reg.register(SlashCommand(
        name="agents",
        description="List teammates spawned in this project (alive + recently stopped).",
        handler=_cmd_agents,
    ))