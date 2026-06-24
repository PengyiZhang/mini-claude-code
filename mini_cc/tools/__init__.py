"""Tool registry. Built per-session so handlers can close over ctx."""
from __future__ import annotations

from typing import Iterable

from .base import FunctionTool, Tool, ToolContext
from . import bash as _bash
from . import background as _background
from . import bgtask as _bgtask
from . import cron as _cron
from . import fs as _fs
from . import mcp as _mcp
from . import repl as _repl
from . import skills as _skills
from . import subagent as _subagent
from . import task as _task
from . import teams as _teams
from . import todo as _todo
from . import wakeup as _wakeup
from . import web as _web
from . import websearch as _websearch
from . import workflow as _workflow
from . import worktree as _worktree


def builtin_tools() -> list[Tool]:
    """Return the default built-in tools. Teams are layered on later."""
    from ..lsp import LSP_TOOL
    return [*_fs.ALL, *_bash.ALL, *_todo.ALL, *_skills.ALL,
            *_cron.ALL, *_mcp.ALL, *_task.ALL, *_worktree.ALL,
            *_teams.ALL, *_subagent.ALL,
            *_web.ALL, *_bgtask.ALL, *_websearch.ALL, *_wakeup.ALL,
            *_workflow.ALL, *_repl.ALL, LSP_TOOL]


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
