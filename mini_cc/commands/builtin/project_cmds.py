"""Project-state slash commands (tasks, compact, cost, permissions, logs) (split from commands/registry.py — M3-1)."""
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

from ._util import _loop_of

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
    from ...core.compaction import compact_history
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
    from ...core.hooks import DENY_LIST, DESTRUCTIVE
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



def register(reg) -> None:

    reg.register(SlashCommand(
        name="compact",
        description="Manually trigger history compaction.",
        handler=_cmd_compact,
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
        name="logs",
        description="List recent log files under mini_cc/logs/.",
        handler=_cmd_logs,
    ))