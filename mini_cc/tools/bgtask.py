"""Background task inspection tools — task_output + task_stop.

These complement the bash tool's ``run_in_background=True`` mode and
the loop's auto-detect slow-op offload. Both look up the task on the
project's BackgroundScheduler (attached to ToolContext).
"""
from __future__ import annotations

from .base import FunctionTool, ToolContext


def _task_output(ctx: ToolContext, args: dict) -> str:
    bg = ctx.background_scheduler
    if bg is None:
        return ("Error: no BackgroundScheduler attached to this project. "
                "Run commands synchronously instead.")
    bg_id = (args.get("bg_id") or "").strip()
    if not bg_id:
        return "Error: bg_id is required"
    return bg.get_output(bg_id)


def _task_stop(ctx: ToolContext, args: dict) -> str:
    bg = ctx.background_scheduler
    if bg is None:
        return ("Error: no BackgroundScheduler attached to this project.")
    bg_id = (args.get("bg_id") or "").strip()
    if not bg_id:
        return "Error: bg_id is required"
    return bg.stop(bg_id)


TASK_OUTPUT_TOOL = FunctionTool(
    name="task_output",
    description=(
        "Read the current output of a background task started via "
        "bash run_in_background=true or the loop's slow-op offload. "
        "Returns the result if the task has completed, or a running "
        "status placeholder otherwise. The result is NOT drained — "
        "the next turn will still surface it as a task_notification."),
    input_schema={
        "type": "object",
        "properties": {
            "bg_id": {
                "type": "string",
                "description": "Background task ID (bg_xxxxxxxx).",
            },
        },
        "required": ["bg_id"],
    },
    fn=_task_output,
)


TASK_STOP_TOOL = FunctionTool(
    name="task_stop",
    description=(
        "Best-effort cancel of a running background task. Signals the "
        "task's cancel_event so the sandbox terminates the underlying "
        "subprocess (SIGTERM → SIGKILL on POSIX, TerminateProcess on "
        "Windows) within ~100ms. Marks the task as stopped so its "
        "output is suppressed in subsequent task_notification blocks."),
    input_schema={
        "type": "object",
        "properties": {
            "bg_id": {
                "type": "string",
                "description": "Background task ID (bg_xxxxxxxx).",
            },
        },
        "required": ["bg_id"],
    },
    fn=_task_stop,
)


ALL = [TASK_OUTPUT_TOOL, TASK_STOP_TOOL]
