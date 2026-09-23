"""Workflow slash commands (split from commands/registry.py — M3-1)."""
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
    from ...workflow import workflow_from_dict
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



def register(reg) -> None:

    reg.register(SlashCommand(
        name="workflow",
        description="Show the active workflow's status. `/workflow clear` drops it.",
        handler=_cmd_workflow,
    ))