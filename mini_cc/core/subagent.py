"""Subagent dispatch — focused single-purpose agent runs.

Ports s20 lines 962-1052. A subagent is a fresh AgentLoop with a
restricted tool set (filesystem + bash only — no task/team/cron/etc.)
that runs to completion and returns its final text summary.

Used by the `task` tool so the main agent can dispatch focused work
without polluting its own context.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loop import ProjectRef


# Tools a subagent is allowed to see. Matches s20's SUB_TOOLS list plus
# `grep` (s20 didn't expose it to subagents but it's a natural fit and
# already routed through the sandbox). Filesystem tools in mini_cc use
# the `_file` suffix (read_file / write_file / edit_file).
SUBAGENT_TOOL_NAMES = frozenset({
    "bash", "read_file", "write_file", "edit_file", "glob", "grep",
})


def _build_subagent_prompt(project_root: str) -> str:
    return (
        f"You are a coding subagent at {project_root}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )


def spawn_subagent(project: "ProjectRef", description: str,
                   *,
                   max_tool_calls: int = 30,
                   client_factory=None,
                   model: str | None = None,
                   on_event=None,
                   session_id_out: list[str] | None = None) -> str:
    """Run a focused subagent to completion. Returns its final text summary.

    The subagent uses a fresh AgentLoop with a per-call session_id and a
    frozen tool list (filesystem + bash). The loop drives itself to
    completion; if it exceeds `max_tool_calls` tool invocations we stop
    early and return whatever summary text was last produced.

    ``on_event`` is forwarded to the inner AgentLoop so the parent loop
    can observe the subagent's tool_use / tool_result events and surface
    them as live activities in the parent's SSE stream. Without this,
    subagent work is invisible to the user except for the final summary
    text that this function returns.

    ``session_id_out`` is an optional 1-element list the caller can use
    to receive the subagent's session_id (e.g. so the parent's `task`
    tool result can reference it for debugging / observability). The id
    is also embedded in the loop's storage transcript, so callers that
    don't need it can ignore the parameter.
    """
    # Local imports to avoid a circular dependency at module load time
    # (tools imports core.subagent via tools.subagent).
    from .loop import AgentLoop
    from ..tools import builtin_tools

    sub_tools = [t for t in builtin_tools()
                 if t.name in SUBAGENT_TOOL_NAMES]
    # Hyphen, not colon: HTTP path validation (server/deps.py validate_id)
    # only accepts [A-Za-z0-9_-]+, so a "subagent:xxx" id would 400 any
    # client that tried to open it. Hyphen keeps the prefix recognizable
    # while staying URL-safe.
    session_id = f"subagent-{uuid.uuid4().hex[:8]}"
    if session_id_out is not None:
        session_id_out.append(session_id)
    loop = AgentLoop(
        project, session_id,
        tools=sub_tools,
        system_prompt_override=_build_subagent_prompt(project.project_root),
        model=model,
        hooks=None,  # subagent runs without user-facing hooks
        on_event=on_event,
    )
    if client_factory is not None:
        loop._client = client_factory()  # type: ignore[attr-defined]

    final_text = ""
    tool_calls = 0
    stopped_early = False

    for ev in loop.run(description):
        etype = ev.get("type")
        if etype == "text":
            final_text = ev.get("text", "") or final_text
        elif etype == "tool_use":
            tool_calls += 1
            if tool_calls >= max_tool_calls:
                loop.stop()
                stopped_early = True
        elif etype == "error":
            return f"[subagent error] {ev.get('message', '')}"

    if stopped_early and not final_text:
        return ("Subagent hit the tool-call cap without producing a "
                "summary.")
    return final_text or "Subagent finished without a text summary."
