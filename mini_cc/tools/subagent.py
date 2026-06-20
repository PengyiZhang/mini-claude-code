"""Subagent dispatch tool — exposes spawn_subagent to the main agent.

The tool name is `task` to match s20. It dispatches a focused subagent
with a restricted toolset (filesystem + bash) and returns its summary.
"""
from __future__ import annotations

from ..core.subagent import spawn_subagent
from .base import FunctionTool, ToolContext


def _task(ctx: ToolContext, args: dict) -> str:
    if ctx.project_ref is None:
        return "Subagent dispatch requires a ProjectRef on the ToolContext"
    return spawn_subagent(
        ctx.project_ref,
        args["description"],
        client_factory=ctx.subagent_client_factory,
    )


TASK_TOOL = FunctionTool(
    name="task",
    description=("Launch a focused subagent to complete a described task. "
                 "Returns only its final summary."),
    input_schema={
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "required": ["description"],
    },
    fn=_task,
)

ALL = [TASK_TOOL]
