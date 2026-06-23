"""Bash tool. Routes through ctx.sandbox.execute.

Supports three knobs the agent can dial:
- ``timeout`` — per-call ceiling (default 120s, hard cap 600s). Long
  builds or test suites can opt up; anything tighter than the default
  surfaces failures faster.
- ``cwd`` — subdirectory of project_root to chdir into before running.
  Validated by the sandbox to stay inside the project.
- ``run_in_background`` — hand the command to the project's
  BackgroundScheduler and return a bg_id immediately. The result lands
  as a <task_notification> in a later turn.
"""
from __future__ import annotations

from .base import FunctionTool, ToolContext
from ..sandbox import CommandBlockedError


# Hard ceiling on per-call timeout. Even when an agent asks for more we
# refuse — anything beyond this should be a background task.
MAX_TIMEOUT_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 120


def _bash(ctx: ToolContext, args: dict) -> str:
    command = args["command"]
    run_in_background = bool(args.get("run_in_background", False))
    timeout = _coerce_timeout(args.get("timeout"))
    cwd = args.get("cwd")

    if run_in_background:
        # The BackgroundScheduler is owned by the project, not the
        # sandbox. We require it to be wired into the ToolContext via
        # the loop (see AgentLoop._execute_tool_calls — it diverts
        # slow/bash-background calls before reaching this handler, so
        # getting here with run_in_background=True means either the
        # loop's auto-detect was disabled or the project has no
        # scheduler). Be explicit about either case so the agent can
        # react instead of silently running sync.
        bg = getattr(ctx, "background_scheduler", None)
        if bg is None:
            return ("Error: run_in_background requested but no "
                    "BackgroundScheduler is attached to this project. "
                    "Run synchronously instead.")
        tool_use_id = str(args.get("__tool_use_id", "bg"))
        bg_id = bg.start_bg(ctx, "bash",
                            {"command": command, "timeout": timeout, "cwd": cwd},
                            tool_use_id, command_str=command)
        return (f"[Background task {bg_id} started] "
                "Result will arrive as a task_notification.")

    try:
        r = ctx.sandbox.execute(command, timeout=timeout, cwd=cwd)
    except CommandBlockedError as e:
        return f"Error: command blocked: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    out = (r.stdout + r.stderr).strip()
    if len(out) > 50000:
        out = out[:50000] + f"\n... ({len(out) - 50000} more bytes)"
    return out if out else "(no output)"


def _coerce_timeout(raw) -> int:
    """Clamp the requested timeout to [1, MAX_TIMEOUT_SECONDS].

    Bogus inputs (None, negative, non-numeric) fall back to the default.
    """
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if v <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(v, MAX_TIMEOUT_SECONDS)


BASH_TOOL = FunctionTool(
    name="bash",
    description=(
        "Run a shell command in the project sandbox. Prefers a real bash on "
        "PATH (Git Bash on Windows) so Unix syntax works everywhere: "
        "mkdir -p, ls -la, pipes, &&, $VAR, rm -rf, curl, jq, grep, find. "
        "If no bash is found, falls back to the host shell (cmd.exe on "
        "Windows) — in that case prefer cross-platform flags. Output is "
        "decoded as UTF-8 with errors replaced, so commands emitting non-"
        "ASCII bytes won't crash the sandbox. "
        "Optional `timeout` (seconds, max 600) and `cwd` (subdir of "
        "project root) tune execution. `run_in_background=true` hands "
        "the command to the project's BackgroundScheduler and returns a "
        "bg_id; output arrives as a task_notification in a later turn."),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "run_in_background": {"type": "boolean"},
            "timeout": {
                "type": "integer",
                "description": "Per-call timeout in seconds (1-600). "
                               "Defaults to 120.",
            },
            "cwd": {
                "type": "string",
                "description": "Subdirectory of project root to run in. "
                               "Must stay inside the project.",
            },
        },
        "required": ["command"],
    },
    fn=_bash,
)


ALL = [BASH_TOOL]
