"""Cron tools: schedule_cron, list_crons, cancel_cron."""
from __future__ import annotations

from .base import FunctionTool, ToolContext


def _schedule(ctx: ToolContext, args: dict) -> str:
    if ctx.scheduler is None:
        return "Error: scheduler not configured for this project"
    job, err = ctx.scheduler.schedule(
        cron=args["cron"],
        prompt=args["prompt"],
        recurring=bool(args.get("recurring", True)),
        durable=bool(args.get("durable", True)),
    )
    if err:
        return f"Error: {err}"
    return (f"Scheduled {job.job_id}: '{job.cron}' -> {job.prompt}")


def _list(ctx: ToolContext, args: dict) -> str:
    if ctx.scheduler is None:
        return "Error: scheduler not configured for this project"
    jobs = ctx.scheduler.list_jobs()
    if not jobs:
        return "No cron jobs."
    return "\n".join(
        f"  {j.job_id}: '{j.cron}' -> {j.prompt[:40]} "
        f"[{'recurring' if j.recurring else 'one-shot'}, "
        f"{'durable' if j.durable else 'session'}]"
        for j in jobs)


def _cancel(ctx: ToolContext, args: dict) -> str:
    if ctx.scheduler is None:
        return "Error: scheduler not configured for this project"
    return ctx.scheduler.cancel(args["job_id"])


SCHEDULE_TOOL = FunctionTool(
    name="schedule_cron",
    description=("Schedule a cron job. cron is 5-field: min hour dom month dow. "
                 "For one-shot reminders, set recurring=false."),
    input_schema={
        "type": "object",
        "properties": {
            "cron": {"type": "string"},
            "prompt": {"type": "string"},
            "recurring": {"type": "boolean"},
            "durable": {"type": "boolean"},
        },
        "required": ["cron", "prompt"],
    },
    fn=_schedule,
)

LIST_TOOL = FunctionTool(
    name="list_crons",
    description="List registered cron jobs for this project.",
    input_schema={"type": "object", "properties": {}, "required": []},
    fn=_list,
)

CANCEL_TOOL = FunctionTool(
    name="cancel_cron",
    description="Cancel a cron job by ID.",
    input_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    fn=_cancel,
)

ALL = [SCHEDULE_TOOL, LIST_TOOL, CANCEL_TOOL]
