"""Scheduling slash commands (/loop, /bg) (split from commands/registry.py — M3-1)."""
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



def register(reg) -> None:

    reg.register(SlashCommand(
        name="loop",
        description="List scheduled cron + wakeup jobs. `/loop cancel <id>` removes one.",
        handler=_cmd_loop,
    ))
    reg.register(SlashCommand(
        name="bg",
        description="List background tasks. `/bg stop <bg_id>` cancels one.",
        handler=_cmd_bg,
    ))