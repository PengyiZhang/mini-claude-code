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
    # P0-6: surface the subagent's session_id in the tool_result so the
    # parent (and observability / resume tooling) can correlate the
    # summary with the subagent's own transcript on disk. spawn_subagent
    # writes the id into this 1-element list as a side channel so the
    # summary text itself stays model-friendly.
    session_id_out: list[str] = []
    summary = spawn_subagent(
        ctx.project_ref,
        args["description"],
        client_factory=ctx.subagent_client_factory,
        on_event=ctx.on_subagent_event,
        session_id_out=session_id_out,
        allow_mcp=bool(args.get("allow_mcp", False)),
    )
    sid = session_id_out[0] if session_id_out else "subagent-unknown"
    return f"{summary}\n\n[subagent_session_id: {sid}]"


TASK_TOOL = FunctionTool(
    name="task",
    description=("Launch a focused subagent to complete a described task. "
                 "Returns only its final summary. Set allow_mcp=true to "
                 "give the subagent access to the project's MCP tools "
                 "(default off — keeps the subagent's blast radius "
                 "minimal)."),
    input_schema={
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "allow_mcp": {
                "type": "boolean",
                "description": "If true, expose MCP tools (mcp__*) to "
                               "the subagent. Default false.",
            },
        },
        "required": ["description"],
    },
    fn=_task,
)

ALL = [TASK_TOOL]
