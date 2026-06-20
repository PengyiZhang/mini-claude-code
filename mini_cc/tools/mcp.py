"""connect_mcp tool — connect this project to a registered MCP server."""
from __future__ import annotations

from .base import FunctionTool, ToolContext


def _connect_mcp(ctx: ToolContext, args: dict) -> str:
    if ctx.mcp_pool is None:
        return "Error: MCP not configured for this project"
    name = args["name"]
    ok, msg = ctx.mcp_pool.connect(name)
    if not ok:
        return f"Error: {msg}"
    return msg


CONNECT_MCP_TOOL = FunctionTool(
    name="connect_mcp",
    description=("Connect this project to an MCP server. After connecting, "
                 "that server's tools become available as mcp__server__tool."),
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    fn=_connect_mcp,
)

ALL = [CONNECT_MCP_TOOL]
