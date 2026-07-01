"""Teams tools: send_message, check_inbox, list_teammates, request_shutdown,
spawn_teammate, submit_plan, request_plan, review_plan.

All routed through ctx.teams (TeammateSpawner) which owns the project's
MessageBus + ProtocolTracker. The lead agent uses these; teammates get
their own send_message / submit_plan via the spawner's sub-AgentLoop.
"""
from __future__ import annotations

import json

from .base import FunctionTool, ToolContext


_TEAMMATE_PREFIX = "teammate-"


def _name_from_session(ctx: ToolContext) -> str | None:
    sid = ctx.session_id or ""
    if sid.startswith(_TEAMMATE_PREFIX):
        return sid[len(_TEAMMATE_PREFIX):]
    return None


def _send(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    to = args["to"]
    content = args["content"]
    msg_type = args.get("msg_type", "message")
    # Teammates identify themselves by their session name; the lead uses
    # the literal "lead".
    from_ = _name_from_session(ctx) or "lead"
    # debug.7.md Task 1c: lead-side @mention must re-bind the teammate's
    # event sink so events from the wake-up turn flow into the current
    # SSE stream (the spawn-time sink went stale when spawn_teammate
    # returned). Teammate-to-teammate sends don't need this — their
    # events already route through their own runner's sink.
    if from_ == "lead" and hasattr(spawner, "bind_event_sink"):
        spawner.bind_event_sink(to, ctx.on_subagent_event)
    spawner.bus.send(from_, to, content, msg_type)
    # debug.7.md Task 1e: warn when sending to a known-but-stopped
    # teammate — the message is queued in the mailbox but won't be
    # processed until the teammate is restarted. Without this hint the
    # user stared at a silent tool_result and confused it with stale
    # activities from the original spawn.
    if from_ == "lead":
        registry = getattr(spawner, "_teammates", {}) or {}
        info = registry.get(to)
        if info is not None and not getattr(info, "alive", False):
            return (f"Sent to {to} (queued — {to} is stopped; "
                    "restart via /agents spawn to process)")
    return f"Sent to {to}"


def _check_inbox(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    who = _name_from_session(ctx) or "lead"
    msgs = spawner.bus.read_inbox(who)
    if not msgs:
        return "Inbox empty."
    return json.dumps(msgs, ensure_ascii=False)


def _list(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    alive = spawner.list_alive()
    if not alive:
        return "No active teammates."
    return "\n".join(f"  {t.name}: {t.role}" for t in alive)


def _shutdown(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    return spawner.request_shutdown(args["name"])


def _spawn(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    # P0-7: depth-1 recursion cap. Teammates are built with the same
    # builtin_tools() set as the lead, so without this guard a teammate
    # could spawn its own teammates, each of which could spawn more,
    # fanning out unboundedly. The session_id prefix ("teammate-...")
    # is set by TeammateSpawner._runner and reliably identifies threads
    # running inside a teammate loop — refuse the call there and ask the
    # lead to spawn instead.
    if _name_from_session(ctx) is not None:
        return ("Teammates cannot spawn their own teammates (recursion "
                "cap). Ask the lead to spawn instead.")
    # Pass the parent loop's event sink through so the lead's UI sees the
    # teammate's tool_use / tool_result activities stream in as they run.
    # TeammateSpawner.spawn already accepts on_event; the previous lead
    # tool simply wasn't wiring it up. (Note: teammates run in a separate
    # daemon thread, so events are delivered concurrently — the sink must
    # be thread-safe. ctx.on_subagent_event is set up per-call by
    # AgentLoop._execute_tool_calls; if a teammate outlives the parent
    # tool's call, late events are dropped once the sink is cleared.)
    on_event = ctx.on_subagent_event
    err = spawner.spawn(args["name"], args.get("role", "agent"),
                        args["prompt"], on_event=on_event)
    if err is not None:
        return err
    return f"Teammate '{args['name']}' spawned"


def _submit_plan(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    name = _name_from_session(ctx)
    if name is None:
        return "submit_plan is only available to teammates"
    plan = args["plan"]
    req_id = spawner.submit_plan(name, plan)
    return (f"Plan submitted ({req_id}). "
            "End your turn and wait for approval.")


def _request_plan(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    return spawner.request_plan(args["teammate"], args["task"])


def _review_plan(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    return spawner.review_plan(
        args["request_id"], bool(args["approve"]),
        args.get("feedback", ""))


SEND_TOOL = FunctionTool(
    name="send_message",
    description="Send a message to another agent's mailbox.",
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "content": {"type": "string"},
            "msg_type": {"type": "string"},
        },
        "required": ["to", "content"],
    },
    fn=_send,
)

CHECK_TOOL = FunctionTool(
    name="check_inbox",
    description="Drain and return all messages in the caller's inbox (lead or teammate).",
    input_schema={"type": "object", "properties": {}, "required": []},
    fn=_check_inbox,
)

LIST_TOOL = FunctionTool(
    name="list_teammates",
    description="List active teammates for this project.",
    input_schema={"type": "object", "properties": {}, "required": []},
    fn=_list,
)

SHUTDOWN_TOOL = FunctionTool(
    name="request_shutdown",
    description="Send a shutdown_request to a teammate.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    fn=_shutdown,
)

SPAWN_TOOL = FunctionTool(
    name="spawn_teammate",
    description="Spawn a teammate that runs the prompt in a background thread.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "role": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["name", "prompt"],
    },
    fn=_spawn,
)

SUBMIT_PLAN_TOOL = FunctionTool(
    name="submit_plan",
    description=("Submit a plan to the lead for approval. Teammates only. "
                 "After calling this, end your turn and wait."),
    input_schema={
        "type": "object",
        "properties": {"plan": {"type": "string"}},
        "required": ["plan"],
    },
    fn=_submit_plan,
)

REQUEST_PLAN_TOOL = FunctionTool(
    name="request_plan",
    description="Ask a teammate to submit a plan for a task.",
    input_schema={
        "type": "object",
        "properties": {
            "teammate": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["teammate", "task"],
    },
    fn=_request_plan,
)

REVIEW_PLAN_TOOL = FunctionTool(
    name="review_plan",
    description="Approve or reject a pending plan_approval_request by id.",
    input_schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "approve": {"type": "boolean"},
            "feedback": {"type": "string"},
        },
        "required": ["request_id", "approve"],
    },
    fn=_review_plan,
)

ALL = [SEND_TOOL, CHECK_TOOL, LIST_TOOL, SHUTDOWN_TOOL, SPAWN_TOOL,
       SUBMIT_PLAN_TOOL, REQUEST_PLAN_TOOL, REVIEW_PLAN_TOOL]

