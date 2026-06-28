"""Per-project hook registry + built-in permission hook.

Ports s20 lines 874-959 with two key differences:
- Hooks are per-project instances, not module globals.
- The s20 permission_hook prompts interactively via input(); that
  doesn't fit a backend/SDK context. make_permission_hook() returns a
  non-interactive hook that denies destructive commands outright unless
  allow_destructive=True. Applications wanting interactive prompts can
  register their own hook.

Hook event signatures (callbacks receive these args):
- UserPromptSubmit(query: str) -> Optional[str]
    A non-None return replaces the user's query.
- PreToolUse(tool_name: str, tool_input: dict) -> Optional[str]
    A non-None return denies the call; the string becomes the
    tool_result content shown to the model.
- PostToolUse(tool_name: str, tool_input: dict, output: str) -> None
- Stop() -> None
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable


logger = logging.getLogger(__name__)


class Hooks:
    """Per-project hook registry."""

    UserPromptSubmit = "UserPromptSubmit"
    PreToolUse = "PreToolUse"
    PostToolUse = "PostToolUse"
    Stop = "Stop"

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = defaultdict(list)

    def register(self, event: str, callback: Callable) -> None:
        self._hooks[event].append(callback)

    def clear(self, event: str | None = None) -> None:
        if event is None:
            self._hooks.clear()
        else:
            self._hooks.pop(event, None)

    def trigger(self, event: str, *args) -> Any:
        """Call each callback in registration order. Returns the first
        non-None result; None if no callback returned anything.

        B3: each callback is wrapped in try/except so a buggy hook
        can't take down the whole turn. The exception is logged and
        the hook is skipped; downstream hooks still run.
        """
        for cb in self._hooks.get(event, ()):
            try:
                result = cb(*args)
            except Exception as e:
                logger.exception(
                    "hook.exception event=%s callback=%s error=%s",
                    event, getattr(cb, "__name__", repr(cb)), e)
                continue
            if result is not None:
                return result
        return None

    def has(self, event: str) -> bool:
        return bool(self._hooks.get(event))


# Patterns nearly always unintentional in agent bash invocations. The
# SubprocessSandbox already blocks most of these via Policy; this hook
# adds a defense-in-depth layer that applications can extend with
# arbitrary Python logic.
DENY_LIST = ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")
DESTRUCTIVE = ("rm ", "> /etc/", "chmod 777")


def make_permission_hook(*, allow_destructive: bool = False,
                         deny_list: tuple[str, ...] = DENY_LIST,
                         destructive: tuple[str, ...] = DESTRUCTIVE,
                         ) -> Callable[[str, dict], str | None]:
    """Build a PreToolUse hook that blocks dangerous bash commands.

    Denies anything in `deny_list` outright. If `allow_destructive` is
    False (the default), also denies anything matching `destructive`.
    """
    def hook(tool_name: str, tool_input: dict) -> str | None:
        if tool_name != "bash":
            return None
        cmd = (tool_input or {}).get("command", "")
        for pat in deny_list:
            if pat in cmd:
                return f"Permission denied: '{pat}' is on the deny list"
        if not allow_destructive:
            for tok in destructive:
                if tok in cmd:
                    return ("Permission denied: destructive command "
                            f"(matched '{tok}'); override with "
                            "allow_destructive=True")
        return None
    return hook


def make_log_hook(sink: Callable[[str, dict], None]
                  ) -> Callable[[str, dict], None]:
    """PreToolUse hook that forwards every tool invocation to `sink`
    as `sink(name, tool_input)`. Returns None (never blocks) so it
    composes cleanly with permission hooks. Ports s20's log_hook.

    Use for audit / telemetry / tracing.
    """
    def hook(tool_name: str, tool_input: dict) -> None:
        sink(tool_name, tool_input or {})
    return hook


def make_large_output_hook(threshold: int = 100_000,
                           sink: Callable[[str, int], None] = print
                           ) -> Callable[[str, dict, str], None]:
    """PostToolUse hook that calls `sink(tool_name, output_len)` when
    a tool's output exceeds `threshold` characters. Ports s20's
    large_output_hook. Returns None.

    The hook signals only; truncation happens later via tool_result_budget
    in the compaction layer.
    """
    def hook(tool_name: str, tool_input: dict, output: str) -> None:
        n = len(str(output))
        if n > threshold:
            sink(tool_name, n)
    return hook


def make_audit_hook(sink: Callable[[str, dict], None]
                    ) -> "Hooks":
    """Convenience: build a Hooks registry pre-wired to send every event
    to a single `sink(event_name, payload)` callable. The payload dict
    keys vary by event:

    - UserPromptSubmit: {"query": str}
    - PreToolUse:       {"name": str, "input": dict}
    - PostToolUse:      {"name": str, "input": dict, "output": str}
    - Stop:             {} (no payload)

    None of the callbacks return anything, so they never block or
    rewrite; pair with a permission hook if you also need to enforce.
    """
    h = Hooks()
    h.register(Hooks.UserPromptSubmit,
               lambda q: sink(Hooks.UserPromptSubmit, {"query": q}))
    h.register(Hooks.PreToolUse,
               lambda n, i: sink(Hooks.PreToolUse, {"name": n, "input": i}))
    h.register(Hooks.PostToolUse,
               lambda n, i, o: sink(Hooks.PostToolUse,
                                    {"name": n, "input": i, "output": o}))
    h.register(Hooks.Stop,
               lambda: sink(Hooks.Stop, {}))
    return h
