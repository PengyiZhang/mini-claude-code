"""Tool abstraction. Every tool is a (name, schema, handler) triple bound to
a ToolContext that carries per-project sandbox + storage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Optional, TYPE_CHECKING

from ..sandbox import Sandbox
from ..storage import Storage

if TYPE_CHECKING:
    from ..core.loop import ProjectRef
    from ..mcp import MCPPool
    from ..skills import SkillLoader
    from ..scheduler import CronScheduler
    from ..teams import TeammateSpawner


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
    scheduler: Optional["CronScheduler"] = None
    mcp_pool: Optional["MCPPool"] = None
    teams: Optional["TeammateSpawner"] = None
    # Subagent dispatch: filled in by AgentLoop._make_ctx so the `task`
    # tool can spawn a focused sub-agent against the same project.
    project_ref: Optional["ProjectRef"] = None
    subagent_client_factory: Optional[Callable] = None


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
