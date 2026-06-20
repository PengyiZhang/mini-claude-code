"""Per-project background task scheduler.

Ports s20 lines 1259-1327 with these changes:
- No module-global background_tasks / background_results / background_lock —
  instance state on BackgroundScheduler
- start() takes a (tool_name, tool_input, tool_use_id) triple plus the
  project's ToolContext so the worker can dispatch through the same sandbox
- collect_notifications() drains completed tasks and returns <task_notification>
  XML blocks for injection into the next LLM turn
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from .base import ToolContext


SLOW_KEYWORDS = ("install", "build", "test", "deploy", "compile",
                 "docker build", "pip install", "npm install",
                 "cargo build", "pytest", "make")


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = (tool_input or {}).get("command", "").lower()
    return any(k in command for k in SLOW_KEYWORDS)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    return bool((tool_input or {}).get("run_in_background")) or is_slow_operation(
        tool_name, tool_input)


@dataclass
class _BGTask:
    bg_id: str
    tool_use_id: str
    tool_name: str
    command: str
    status: str = "running"
    result: str = ""


class BackgroundScheduler:
    """Per-project background task pool."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, _BGTask] = {}

    def start(self, ctx: ToolContext, tools: dict, tool_name: str,
              tool_input: dict, tool_use_id: str) -> str:
        """Schedule a tool call in the background. Returns bg_id."""
        bg_id = f"bg_{uuid.uuid4().hex[:8]}"
        command = (tool_input or {}).get("command", tool_name)
        task = _BGTask(bg_id=bg_id, tool_use_id=tool_use_id,
                       tool_name=tool_name, command=str(command))
        with self._lock:
            self._tasks[bg_id] = task

        def worker():
            tool = tools.get(tool_name)
            output = ("Unknown tool" if tool is None
                      else tool.handle(ctx, tool_input or {}))
            with self._lock:
                if bg_id in self._tasks:
                    self._tasks[bg_id].status = "completed"
                    self._tasks[bg_id].result = str(output)

        threading.Thread(target=worker, daemon=True).start()
        return bg_id

    def collect_notifications(self) -> list[str]:
        """Drain completed tasks and return <task_notification> blocks."""
        with self._lock:
            ready = [bg_id for bg_id, t in self._tasks.items()
                     if t.status == "completed"]
            drained = [self._tasks.pop(bg_id) for bg_id in ready]
        out = []
        for task in drained:
            summary = task.result[:200]
            out.append(
                f"<task_notification>\n"
                f"  <task_id>{task.bg_id}</task_id>\n"
                f"  <status>completed</status>\n"
                f"  <command>{task.command}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"</task_notification>")
        return out
