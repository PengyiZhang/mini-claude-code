"""load_skill tool — returns full content of a skill by name."""
from __future__ import annotations

from .base import FunctionTool, ToolContext


def _load_skill(ctx: ToolContext, args: dict) -> str:
    name = args["name"]
    if ctx.skills_loader is None:
        return "Error: skills not configured for this project"
    return ctx.skills_loader.load(name)


LOAD_SKILL_TOOL = FunctionTool(
    name="load_skill",
    description="Load the full content of a skill by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    fn=_load_skill,
)

ALL = [LOAD_SKILL_TOOL]
