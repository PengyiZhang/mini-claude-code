"""schedule_wakeup tool — second-precision self-paced loop hook.

The agent uses this when it needs to check back on something after a
short, known delay: polling a build, pacing a /loop iteration without
blocking, or waking up after an external event the user will trigger.

Distinct from schedule_cron (minute-precision cron expressions, durable
across restarts). Wakeups are in-memory only and tied to the running
loop; if the process restarts, pending wakeups vanish.
"""
from __future__ import annotations

from .base import FunctionTool, ToolContext
from ..scheduler import WakeupScheduler


# Hard ceiling on delay so a buggy caller can't pin a wakeup hours into
# the future (use cron for that). 1 hour is generous for any reasonable
# self-pacing need.
MAX_DELAY_SECONDS = 3600


def _schedule_wakeup(ctx: ToolContext, args: dict) -> str:
    sched = _resolve_scheduler(ctx)
    if sched is None:
        return ("Error: no WakeupScheduler attached to this project. "
                "Configure project.wakeups to enable self-paced loops.")
    try:
        delay = float(args.get("delaySeconds", 0))
    except (TypeError, ValueError):
        return "Error: delaySeconds must be a number"
    if delay <= 0:
        return "Error: delaySeconds must be > 0"
    if delay > MAX_DELAY_SECONDS:
        return (f"Error: delaySeconds {delay} exceeds cap {MAX_DELAY_SECONDS}. "
                "Use schedule_cron for long delays.")
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "Error: prompt is required"
    reason = (args.get("reason") or "").strip()
    wakeup_id = sched.schedule(prompt, delay, reason=reason)
    mins = int(delay // 60)
    secs = int(delay % 60)
    return (f"[Wakeup {wakeup_id} scheduled in "
            f"{mins}m{secs}s] "
            f"Prompt will be injected into the next loop iteration "
            f"after the delay.")


def _list_wakeups(ctx: ToolContext, args: dict) -> str:
    sched = _resolve_scheduler(ctx)
    if sched is None:
        return ("Error: no WakeupScheduler attached to this project.")
    import time as _time
    wakeups = sched.list()
    if not wakeups:
        return "_no wakeups scheduled_"
    now = _time.monotonic()
    lines = [f"**Pending wakeups ({len(wakeups)}):**", ""]
    for w in sorted(wakeups, key=lambda x: x.fire_at):
        remaining = max(0, w.fire_at - now)
        reason_tag = f" — {w.reason}" if w.reason else ""
        prompt_preview = (w.prompt[:60] + "…") if len(w.prompt) > 60 else w.prompt
        lines.append(f"- `{w.wakeup_id}` in {int(remaining)}s{reason_tag}")
        lines.append(f"  prompt: {prompt_preview!r}")
    return "\n".join(lines)


def _cancel_wakeup(ctx: ToolContext, args: dict) -> str:
    sched = _resolve_scheduler(ctx)
    if sched is None:
        return "Error: no WakeupScheduler attached to this project."
    wakeup_id = (args.get("wakeup_id") or "").strip()
    if not wakeup_id:
        return "Error: wakeup_id is required"
    if sched.cancel(wakeup_id):
        return f"[Cancelled {wakeup_id}]"
    return f"Error: unknown wakeup_id {wakeup_id}"


def _resolve_scheduler(ctx: ToolContext):
    # Prefer the project-scoped scheduler if the loop wired one in.
    project = getattr(ctx, "project_ref", None)
    sched = getattr(project, "wakeups", None) if project else None
    if sched is None:
        return None
    if isinstance(sched, WakeupScheduler):
        return sched
    # Duck-type: any object with schedule/cancel/list/tick.
    if all(hasattr(sched, m) for m in ("schedule", "cancel", "list", "tick")):
        return sched
    return None


SCHEDULE_WAKEUP_TOOL = FunctionTool(
    name="schedule_wakeup",
    description=(
        "Schedule a one-shot wakeup that injects a prompt into the "
        "current session after a short delay (seconds). Use this for "
        "self-pacing: poll a build, check a background task, or pace "
        "a /loop iteration. Delay is capped at 3600s (1 hour); use "
        "schedule_cron for longer delays or durable schedules. "
        "State is in-memory — does not survive process restarts."),
    input_schema={
        "type": "object",
        "properties": {
            "delaySeconds": {
                "type": "number",
                "description": "Seconds until the wakeup fires (1-3600).",
            },
            "prompt": {
                "type": "string",
                "description": "Prompt to inject when the wakeup fires.",
            },
            "reason": {
                "type": "string",
                "description": "Short description of why the wakeup was scheduled.",
            },
        },
        "required": ["delaySeconds", "prompt"],
    },
    fn=_schedule_wakeup,
)


LIST_WAKEUPS_TOOL = FunctionTool(
    name="list_wakeups",
    description="List all pending wakeups for the current session.",
    input_schema={"type": "object", "properties": {}},
    fn=_list_wakeups,
)


CANCEL_WAKEUP_TOOL = FunctionTool(
    name="cancel_wakeup",
    description="Cancel a pending wakeup by ID.",
    input_schema={
        "type": "object",
        "properties": {"wakeup_id": {"type": "string"}},
        "required": ["wakeup_id"],
    },
    fn=_cancel_wakeup,
)


ALL = [SCHEDULE_WAKEUP_TOOL, LIST_WAKEUPS_TOOL, CANCEL_WAKEUP_TOOL]
