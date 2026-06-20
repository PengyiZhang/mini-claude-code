"""Git worktree tools: create_worktree, remove_worktree, keep_worktree.

Ports s20 lines 172-283 with these changes:
- All git ops routed through ctx.sandbox.git() (no direct subprocess)
- worktrees live under <workspace>/.worktrees/ (inside sandbox root, so the
  path-whitelist still protects them)
- No global events.jsonl — events land in the project's own .state/ tree
  via storage.write_tool_result (reused as an append-only log; see Note).
- bind_task_to_worktree reuses Storage.save_task so it works per-project
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .base import FunctionTool, ToolContext
from ..sandbox import CommandBlockedError

VALID_WT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_worktree_name(name: str) -> str | None:
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def _worktrees_dir(ctx: ToolContext) -> Path:
    return ctx.sandbox.project_root / ".worktrees"


def _log_event(ctx: ToolContext, event_type: str,
               worktree_name: str, task_id: str = "") -> None:
    """Append a worktree event to the project's event log."""
    events = _worktrees_dir(ctx) / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": event_type, "worktree": worktree_name,
                            "task_id": task_id, "ts": time.time()}) + "\n")


def _bind_task_to_worktree(ctx: ToolContext, task_id: str, worktree_name: str) -> None:
    for t in ctx.storage.load_tasks(ctx.project_id):
        if t.id == task_id:
            t.worktree = worktree_name
            ctx.storage.save_task(ctx.project_id, t)
            return


def _count_changes(ctx: ToolContext, path: Path) -> tuple[int, int]:
    """Returns (dirty_files, unpushed_commits) inside the worktree.
    -1 on git error."""
    try:
        r1 = ctx.sandbox.git(["-C", str(path), "status", "--porcelain"])
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = ctx.sandbox.git(["-C", str(path), "log", "@{push}..HEAD",
                              "--oneline"])
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except (CommandBlockedError, Exception):
        return -1, -1


def _create(ctx: ToolContext, args: dict) -> str:
    name = args["name"]
    task_id = args.get("task_id") or ""
    err = _validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    if task_id:
        if not any(t.id == task_id
                   for t in ctx.storage.load_tasks(ctx.project_id)):
            return f"Error: task {task_id} not found"
    path = _worktrees_dir(ctx) / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    try:
        r = ctx.sandbox.git(["worktree", "add", str(path),
                             "-b", f"wt/{name}", "HEAD"])
    except CommandBlockedError as e:
        return f"Error: git blocked: {e}"
    if r.returncode != 0:
        return f"Git error: {(r.stdout + r.stderr).strip()[:500]}"
    if task_id:
        _bind_task_to_worktree(ctx, task_id, name)
    _log_event(ctx, "create", name, task_id)
    return f"Worktree '{name}' created at {path}"


def _remove(ctx: ToolContext, args: dict) -> str:
    name = args["name"]
    discard = bool(args.get("discard_changes", False))
    err = _validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    path = _worktrees_dir(ctx) / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard:
        files, commits = _count_changes(ctx, path)
        if files < 0:
            return "Cannot verify status. Use discard_changes=true to force."
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree.")
    try:
        r1 = ctx.sandbox.git(["worktree", "remove", str(path), "--force"])
    except CommandBlockedError as e:
        return f"Error: git blocked: {e}"
    if r1.returncode != 0:
        return f"Failed to remove worktree '{name}'"
    ctx.sandbox.git(["branch", "-D", f"wt/{name}"])
    _log_event(ctx, "remove", name)
    return f"Worktree '{name}' removed"


def _keep(ctx: ToolContext, args: dict) -> str:
    name = args["name"]
    err = _validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    _log_event(ctx, "keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


CREATE_TOOL = FunctionTool(
    name="create_worktree",
    description=("Create an isolated git worktree under <workspace>/.worktrees/. "
                 "Optional task_id binds the worktree to a task."),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["name"],
    },
    fn=_create,
)

REMOVE_TOOL = FunctionTool(
    name="remove_worktree",
    description="Remove a worktree. Requires discard_changes=true if dirty.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "discard_changes": {"type": "boolean"},
        },
        "required": ["name"],
    },
    fn=_remove,
)

KEEP_TOOL = FunctionTool(
    name="keep_worktree",
    description="Mark a worktree as kept for review (no removal).",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    fn=_keep,
)

ALL = [CREATE_TOOL, REMOVE_TOOL, KEEP_TOOL]
