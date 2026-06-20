"""Filesystem tools: read_file, write_file, edit_file, glob, grep.

All operations go through ctx.sandbox — they cannot be implemented with
direct open()/pathlib, otherwise the sandbox boundary is meaningless.
"""
from __future__ import annotations

import json
from typing import Callable

from .base import FunctionTool, ToolContext


def _read(ctx: ToolContext, args: dict) -> str:
    path = args["path"]
    limit = args.get("limit")
    offset = args.get("offset", 0)
    try:
        return ctx.sandbox.read(path, limit=limit, offset=offset)
    except Exception as e:
        return f"Error: {e}"


def _write(ctx: ToolContext, args: dict) -> str:
    path, content = args["path"], args["content"]
    try:
        ctx.sandbox.write(path, content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def _edit(ctx: ToolContext, args: dict) -> str:
    try:
        return ctx.sandbox.edit(
            args["path"], args["old_text"], args["new_text"],
            replace_all=bool(args.get("replace_all", False)))
    except Exception as e:
        return f"Error: {e}"


def _glob(ctx: ToolContext, args: dict) -> str:
    try:
        matches = ctx.sandbox.glob(args["pattern"])
        return "\n".join(matches) if matches else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _grep(ctx: ToolContext, args: dict) -> str:
    try:
        results = ctx.sandbox.grep(
            args["pattern"],
            output_mode=args.get("output_mode", "files_with_matches"),
            glob=args.get("glob"),
            head_limit=args.get("head_limit", 250))
        if not results:
            return "(no matches)"
        if args.get("output_mode") == "content":
            return "\n".join(f"{r['file']}:{r['line']}: {r['text']}"
                             for r in results)
        if args.get("output_mode") == "count":
            return "\n".join(f"{r['file']}: {r['count']}" for r in results)
        return "\n".join(r["file"] for r in results)
    except Exception as e:
        return f"Error: {e}"


READ_TOOL = FunctionTool(
    name="read_file",
    description="Read file contents under the project root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        },
        "required": ["path"],
    },
    fn=_read,
)

WRITE_TOOL = FunctionTool(
    name="write_file",
    description="Write content to a file under the project root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    fn=_write,
)

EDIT_TOOL = FunctionTool(
    name="edit_file",
    description="Replace exact text in a file once (or all).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_text", "new_text"],
    },
    fn=_edit,
)

GLOB_TOOL = FunctionTool(
    name="glob",
    description="Find files under the project root matching a glob pattern.",
    input_schema={
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
    fn=_glob,
)

GREP_TOOL = FunctionTool(
    name="grep",
    description="Search file contents under the project root.",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "output_mode": {"type": "string",
                            "enum": ["files_with_matches", "content", "count"]},
            "glob": {"type": "string"},
            "head_limit": {"type": "integer"},
        },
        "required": ["pattern"],
    },
    fn=_grep,
)

ALL = [READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL]
