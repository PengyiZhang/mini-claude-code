"""Task system tools: create_task, list_tasks, get_task, claim_task, complete_task.

Storage layer (load_tasks/save_task/delete_task) lives in mini_cc/storage/.
This module adds task tool wrappers + dependency-gating logic (can_start)
that the s20 reference kept inline.
"""
from __future__ import annotations

import random
import time

from .base import FunctionTool, ToolContext
from ..storage import Task


def _create(ctx: ToolContext, args: dict) -> str:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=args["subject"],
        description=args.get("description", ""),
        status="pending",
        owner=None,
        blockedBy=args.get("blockedBy") or [],
    )
    ctx.storage.save_task(ctx.project_id, task)
    deps = f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
    return f"Created {task.id}: {task.subject}{deps}"


def _list(ctx: ToolContext, args: dict) -> str:
    tasks = ctx.storage.load_tasks(ctx.project_id)
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (owner:{t.owner})" if t.owner else "")
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def _get(ctx: ToolContext, args: dict) -> str:
    task_id = args["task_id"]
    for t in ctx.storage.load_tasks(ctx.project_id):
        if t.id == task_id:
            return (f"{t.id}: {t.subject} [{t.status}]\n"
                    f"description: {t.description}\n"
                    f"owner: {t.owner}\n"
                    f"blockedBy: {t.blockedBy}\n"
                    f"worktree: {t.worktree}")
    return f"Error: task {task_id} not found"


def _can_start(ctx: ToolContext, task: Task) -> tuple[bool, str]:
    """Returns (ok, reason). Every blocker must exist and be completed."""
    by_id = {t.id: t for t in ctx.storage.load_tasks(ctx.project_id)}
    for dep_id in task.blockedBy:
        if dep_id not in by_id:
            return False, f"missing dep: {dep_id}"
        if by_id[dep_id].status != "completed":
            return False, f"blocked by: {dep_id}"
    return True, ""


def _claim(ctx: ToolContext, args: dict) -> str:
    task_id = args["task_id"]
    owner = args.get("owner") or "agent"
    for t in ctx.storage.load_tasks(ctx.project_id):
        if t.id == task_id:
            if t.status != "pending":
                return f"Task {task_id} is {t.status}, cannot claim"
            if t.owner:
                return f"Task {task_id} already owned by {t.owner}"
            ok, reason = _can_start(ctx, t)
            if not ok:
                return f"Cannot start — {reason}"
            t.owner = owner
            t.status = "in_progress"
            ctx.storage.save_task(ctx.project_id, t)
            return f"Claimed {t.id} ({t.subject})"
    return f"Error: task {task_id} not found"


def _complete(ctx: ToolContext, args: dict) -> str:
    task_id = args["task_id"]
    for t in ctx.storage.load_tasks(ctx.project_id):
        if t.id == task_id:
            if t.status != "in_progress":
                return f"Task {task_id} is {t.status}, cannot complete"
            t.status = "completed"
            ctx.storage.save_task(ctx.project_id, t)
            # Find newly-unblocked tasks
            unblocked: list[str] = []
            for other in ctx.storage.load_tasks(ctx.project_id):
                if other.status == "pending" and other.id != task_id:
                    ok, _ = _can_start(ctx, other)
                    if ok and other.blockedBy:
                        unblocked.append(other.subject)
            msg = f"Completed {t.id} ({t.subject})"
            if unblocked:
                msg += f"\nUnblocked: {', '.join(unblocked)}"
            return msg
    return f"Error: task {task_id} not found"


CREATE_TOOL = FunctionTool(
    name="create_task",
    description="Create a durable task with optional dependencies.",
    input_schema={
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "blockedBy": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["subject"],
    },
    fn=_create,
)

LIST_TOOL = FunctionTool(
    name="list_tasks",
    description="List all tasks for this project.",
    input_schema={"type": "object", "properties": {}, "required": []},
    fn=_list,
)

GET_TOOL = FunctionTool(
    name="get_task",
    description="Get full task details.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    fn=_get,
)

CLAIM_TOOL = FunctionTool(
    name="claim_task",
    description="Claim a pending task. Blockers must be completed first.",
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "owner": {"type": "string"},
        },
        "required": ["task_id"],
    },
    fn=_claim,
)

COMPLETE_TOOL = FunctionTool(
    name="complete_task",
    description="Mark an in-progress task as completed.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    fn=_complete,
)

ALL = [CREATE_TOOL, LIST_TOOL, GET_TOOL, CLAIM_TOOL, COMPLETE_TOOL]
