"""Teams tools: send_message, check_inbox, list_teammates, request_shutdown.

All routed through ctx.teams (TeammateSpawner) which owns the project's
MessageBus. The lead agent uses these; teammates get their own send_message
via the spawner's sub-AgentLoop.
"""
from __future__ import annotations

import json

from .base import FunctionTool, ToolContext


def _send(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    to = args["to"]
    content = args["content"]
    msg_type = args.get("msg_type", "message")
    spawner.bus.send("lead", to, content, msg_type)
    return f"Sent to {to}"


def _check_inbox(ctx: ToolContext, args: dict) -> str:
    spawner = ctx.teams
    if spawner is None:
        return "Teams subsystem not configured for this project"
    msgs = spawner.bus.read_inbox("lead")
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
    err = spawner.spawn(args["name"], args.get("role", "agent"),
                        args["prompt"])
    if err is not None:
        return err
    return f"Teammate '{args['name']}' spawned"


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
    description="Drain and return all messages in the lead's inbox.",
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

ALL = [SEND_TOOL, CHECK_TOOL, LIST_TOOL, SHUTDOWN_TOOL, SPAWN_TOOL]
