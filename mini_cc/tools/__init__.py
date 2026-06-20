"""Tool registry. Built per-session so handlers can close over ctx."""
from __future__ import annotations

from typing import Iterable

from .base import FunctionTool, Tool, ToolContext
from . import bash as _bash
from . import cron as _cron
from . import fs as _fs
from . import mcp as _mcp
from . import skills as _skills
from . import todo as _todo


def builtin_tools() -> list[Tool]:
    """Return the default built-in tools. Teams are layered on later."""
    return [*_fs.ALL, *_bash.ALL, *_todo.ALL, *_skills.ALL, *_cron.ALL, *_mcp.ALL]


def to_anthropic(tools: Iterable[Tool]) -> list[dict]:
    """Render tools as Anthropic API tool definitions.
    Tool.input_schema is already the inner {"type":"object", ...} schema."""
    return [{"name": t.name,
             "description": t.description,
             "input_schema": t.input_schema}
            for t in tools]


def dispatch(tools: Iterable[Tool]):
    """Build name -> Tool lookup."""
    return {t.name: t for t in tools}


__all__ = [
    "Tool", "FunctionTool", "ToolContext",
    "builtin_tools", "to_anthropic", "dispatch",
]
