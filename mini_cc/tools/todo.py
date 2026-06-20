"""TodoWrite tool. Persists to ctx.todos (saved via storage by the loop)."""
from __future__ import annotations

import ast
import json

from .base import FunctionTool, ToolContext

_VALID = ("pending", "in_progress", "completed")


def _normalize(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in _VALID:
            return None, f"Error: todos[{i}] invalid status '{t['status']}'"
    return todos, None


def _todo_write(ctx: ToolContext, args: dict) -> str:
    todos, err = _normalize(args.get("todos"))
    if err:
        return err
    ctx.todos[:] = todos
    if ctx.mark_todos_updated:
        ctx.mark_todos_updated()
    return f"Updated {len(todos)} todos"


TODO_TOOL = FunctionTool(
    name="todo_write",
    description="Create and manage a task list for the current session.",
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(_VALID)},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    },
    fn=_todo_write,
)

ALL = [TODO_TOOL]
