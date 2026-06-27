"""memory_list / memory_write / memory_recall tools.

Three operations on the project's 3-tier declarative memory store:
  - list: catalog every memory entry (system + tenant + project tiers)
  - write: persist a new note to the project tier (the only tier agents
    can write; system/tenant are admin-owned)
  - recall: keyword search across all entries

The runtime MEMORY.md (auto-saved by some workflows) is **separate** —
it's loaded into the system prompt every turn and not exposed here.
"""
from __future__ import annotations

from pathlib import Path

from .base import FunctionTool, ToolContext


def _project_root(ctx: ToolContext) -> Path:
    return ctx.sandbox.project_root


def _list_memories(ctx: ToolContext, args: dict) -> str:
    loader = ctx.memory_loader
    if loader is None:
        return "Error: memory not configured for this project"
    cat = args.get("category")
    registry = loader.registry
    if not registry:
        return "(no memories found)"
    if cat:
        items = [m for m in registry.values() if m.category == cat]
    else:
        items = list(registry.values())
    if not items:
        return f"(no memories in category '{cat}')"
    return "\n".join(f"- {m.name} [{m.category}]: {m.description}"
                     for m in items)


def _write_memory(ctx: ToolContext, args: dict) -> str:
    from ..memory import write_memory
    name = args["name"]
    content = args["content"]
    category = args.get("category", "general")
    description = args.get("description")
    fp = write_memory(_project_root(ctx), name, content,
                      category=category, description=description)
    return f"Saved memory '{name}' → {fp}"


def _recall_memory(ctx: ToolContext, args: dict) -> str:
    loader = ctx.memory_loader
    if loader is None:
        return "Error: memory not configured for this project"
    query = args["query"]
    matches = loader.recall(query, limit=args.get("limit", 10))
    if not matches:
        return f"No memories matching '{query}'."
    out = [f"Found {len(matches)} memor{'y' if len(matches)==1 else 'ies'} "
           f"for '{query}':"]
    for m in matches:
        out.append(f"\n## {m.name} [{m.category}]\n{m.content}")
    return "\n".join(out)


MEMORY_LIST_TOOL = FunctionTool(
    name="memory_list",
    description=("List declarative memories (system/tenant/project tiers). "
                 "Optional `category` filter (e.g. 'decisions', 'preferences')."),
    input_schema={
        "type": "object",
        "properties": {"category": {"type": "string"}},
    },
    fn=_list_memories,
)

MEMORY_WRITE_TOOL = FunctionTool(
    name="memory_write",
    description=("Persist a note the agent should remember in future "
                 "sessions. Writes to project tier only. Overwrites same-name."),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "filename-safe identifier (e.g. 'auth-decision')"},
            "content": {"type": "string",
                        "description": "markdown body of the memory"},
            "category": {"type": "string",
                         "description": "grouping (decisions/preferences/issues/...)",
                         "default": "general"},
            "description": {"type": "string",
                            "description": "one-line summary; defaults to first line"},
        },
        "required": ["name", "content"],
    },
    fn=_write_memory,
)

MEMORY_RECALL_TOOL = FunctionTool(
    name="memory_recall",
    description="Keyword search across all memories. Returns matching entries ranked by hit count.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
    fn=_recall_memory,
)


ALL = [MEMORY_LIST_TOOL, MEMORY_WRITE_TOOL, MEMORY_RECALL_TOOL]
