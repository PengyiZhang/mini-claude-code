"""Bash tool. Routes through ctx.sandbox.execute; background mode returns
a placeholder (real background scheduler is a later port)."""
from __future__ import annotations

from .base import FunctionTool, ToolContext
from ..sandbox import CommandBlockedError


def _bash(ctx: ToolContext, args: dict) -> str:
    command = args["command"]
    run_in_background = bool(args.get("run_in_background", False))
    if run_in_background:
        # Background task subsystem is not yet ported. Refuse clearly.
        return ("Error: run_in_background is not yet supported in mini_cc "
                "(background scheduler pending). Run synchronously.")
    try:
        r = ctx.sandbox.execute(command, timeout=120)
    except CommandBlockedError as e:
        return f"Error: command blocked: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    out = (r.stdout + r.stderr).strip()
    if len(out) > 50000:
        out = out[:50000] + f"\n... ({len(out) - 50000} more bytes)"
    return out if out else "(no output)"


BASH_TOOL = FunctionTool(
    name="bash",
    description=(
        "Run a shell command in the project sandbox. Prefers a real bash on "
        "PATH (Git Bash on Windows) so Unix syntax works everywhere: "
        "mkdir -p, ls -la, pipes, &&, $VAR, rm -rf, curl, jq, grep, find. "
        "If no bash is found, falls back to the host shell (cmd.exe on "
        "Windows) — in that case prefer cross-platform flags. Output is "
        "decoded as UTF-8 with errors replaced, so commands emitting non-"
        "ASCII bytes won't crash the sandbox."),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "run_in_background": {"type": "boolean"},
        },
        "required": ["command"],
    },
    fn=_bash,
)

ALL = [BASH_TOOL]
