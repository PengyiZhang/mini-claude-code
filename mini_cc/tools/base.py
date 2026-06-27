"""Tool abstraction. Every tool is a (name, schema, handler) triple bound to
a ToolContext that carries per-project sandbox + storage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Optional, TYPE_CHECKING

from ..sandbox import Sandbox
from ..storage import Storage

if TYPE_CHECKING:
    from ..core.loop import ProjectRef
    from ..memory import MemoryLoader
    from ..mcp import MCPPool
    from ..skills import SkillLoader
    from ..scheduler import CronScheduler
    from ..teams import TeammateSpawner
    from .background import BackgroundScheduler


@dataclass
class ToolContext:
    """Per-invocation context handed to every tool handler."""
    project_id: str
    session_id: str
    sandbox: Sandbox
    storage: Storage
    todos: list[dict]
    mark_todos_updated: Callable[[], None] | None = None
    skills_loader: Optional["SkillLoader"] = None
    memory_loader: Optional["MemoryLoader"] = None
    scheduler: Optional["CronScheduler"] = None
    mcp_pool: Optional["MCPPool"] = None
    teams: Optional["TeammateSpawner"] = None
    # Subagent dispatch: filled in by AgentLoop._make_ctx so the `task`
    # tool can spawn a focused sub-agent against the same project.
    project_ref: Optional["ProjectRef"] = None
    subagent_client_factory: Optional[Callable] = None
    # Sink for events emitted by a nested sub-AgentLoop running inside a
    # tool call (currently the `task` tool). AgentLoop._execute_tool_calls
    # sets this per-call to a list-append closure, runs the tool, then
    # drains the list and yields the events into the parent's SSE stream
    # so the user sees subagent tool_use / tool_result activities appear
    # live under the parent's current assistant turn. None on contexts
    # that don't originate from AgentLoop (tests, SDK direct-use).
    on_subagent_event: Optional[Callable[[dict], None]] = None
    # Background task handles. The bash tool uses background_scheduler
    # when the agent asks for run_in_background=true explicitly; the
    # task_output / task_stop tools read against it on every call.
    # background_tools is a shared name->Tool map the worker thread uses
    # to re-enter the handler it was launched from (set by AgentLoop).
    background_scheduler: Optional["BackgroundScheduler"] = None
    background_tools: Optional[dict] = None


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict

    def handle(self, ctx: ToolContext, args: dict) -> str: ...


@dataclass
class FunctionTool:
    """Wrap a plain function as a Tool."""
    name: str
    description: str
    input_schema: dict
    fn: Callable[[ToolContext, dict], str]

    def handle(self, ctx: ToolContext, args: dict) -> str:
        try:
            return self.fn(ctx, args or {})
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"


def schema(name: str, description: str, **properties) -> dict:
    required = [k for k, v in properties.items()
                if isinstance(v, dict) and v.get("__required", False)]
    clean = {k: ({kk: vv for kk, vv in v.items() if kk != "__required"}
                 if isinstance(v, dict) else v)
             for k, v in properties.items()}
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": clean,
            "required": required,
        },
    }
