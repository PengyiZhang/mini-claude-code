"""Per-project background task scheduler.

Ports s20 lines 1259-1327 with these changes:
- No module-global background_tasks / background_results / background_lock —
  instance state on BackgroundScheduler
- start() takes a (tool_name, tool_input, tool_use_id) triple plus the
  project's ToolContext so the worker can dispatch through the same sandbox
- collect_notifications() drains completed tasks and returns <task_notification>
  XML blocks for injection into the next LLM turn

The scheduler also exposes start_bg / get_output / stop so the
``task_output`` and ``task_stop`` tools (and the bash tool's explicit
``run_in_background=True`` path) can drive background work directly
without going through AgentLoop's auto-detect.
"""
from __future__ import annotations

import dataclasses
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
    status: str = "running"          # running | completed | stopped
    result: str = ""
    # The worker thread itself so stop() can join once. None before the
    # thread is launched, may remain None if the worker already exited.
    _thread: threading.Thread | None = field(default=None, repr=False)
    # Cancel signal. Stop() sets this; sandbox.execute() polls it and
    # terminates its subprocess when set. None only briefly between
    # dataclass init and start_bg populating it.
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False)


class BackgroundScheduler:
    """Per-project background task pool."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, _BGTask] = {}

    def start(self, ctx: ToolContext, tools: dict, tool_name: str,
              tool_input: dict, tool_use_id: str) -> str:
        """Schedule a tool call in the background. Returns bg_id.

        Used by AgentLoop's auto-detect path (which already has a tools
        dict in hand). Direct callers from tool handlers should prefer
        :meth:`start_bg`, which takes the input verbatim and looks up
        the handler via ``ctx``-provided tools.
        """
        return self.start_bg(ctx, tool_name, tool_input, tool_use_id,
                             command_str=(tool_input or {}).get("command", tool_name),
                             tools=tools)

    def start_bg(self, ctx: ToolContext, tool_name: str, tool_input: dict,
                 tool_use_id: str, *, command_str: str | None = None,
                 tools: dict | None = None) -> str:
        """Schedule a tool call in the background and return its bg_id.

        ``tools`` is optional — if omitted the worker will reach into
        ``ctx`` for a handler via the loop's _handlers, falling back to
        "Unknown tool" if none is available.
        """
        bg_id = f"bg_{uuid.uuid4().hex[:8]}"
        task = _BGTask(
            bg_id=bg_id, tool_use_id=tool_use_id,
            tool_name=tool_name,
            command=str(command_str if command_str is not None
                        else tool_input.get("command", tool_name)),
        )
        with self._lock:
            self._tasks[bg_id] = task

        # Build a child context with the task's cancel_event attached so
        # sandbox.execute() can observe cancellation. We can't mutate
        # the caller's ctx — it's shared with the foreground loop.
        child_ctx = dataclasses.replace(ctx, cancel_event=task.cancel_event)

        def worker():
            try:
                tool = None
                if tools is not None:
                    tool = tools.get(tool_name)
                if tool is None:
                    # Late lookup via ctx — AgentLoop attaches its
                    # handler table as ctx.background_tools so the bash
                    # tool (which is the typical caller) can find itself
                    # without import cycles.
                    handler_map = getattr(ctx, "background_tools", None)
                    if isinstance(handler_map, dict):
                        tool = handler_map.get(tool_name)
                output = ("Unknown tool" if tool is None
                          else tool.handle(child_ctx, tool_input or {}))
                status = "completed"
            except Exception as e:  # never let the worker thread die silently
                output = f"Error: {type(e).__name__}: {e}"
                status = "completed"
            with self._lock:
                if bg_id in self._tasks and self._tasks[bg_id].status == "running":
                    self._tasks[bg_id].status = status
                    self._tasks[bg_id].result = str(output)

        t = threading.Thread(target=worker, daemon=True)
        task._thread = t
        t.start()
        return bg_id

    def get_output(self, bg_id: str) -> str:
        """Return the current output for a background task.

        - running task → a placeholder describing its state
        - completed task → its result (NOT drained — the next
          ``collect_notifications`` call will still surface it)
        - unknown bg_id → error string
        """
        with self._lock:
            task = self._tasks.get(bg_id)
            if task is None:
                return f"Error: unknown bg_id {bg_id}"
            if task.status == "running":
                return (f"[{bg_id} still running] "
                        f"command: {task.command}")
            return task.result or "(no output)"

    def status(self, bg_id: str) -> str | None:
        """Return the current status string or None if bg_id is unknown."""
        with self._lock:
            task = self._tasks.get(bg_id)
            return task.status if task is not None else None

    def stop(self, bg_id: str, *, timeout: float = 5.0) -> str:
        """Best-effort cancel of a running background task.

        Sets the task's cancel_event; sandbox.execute() that honors the
        event will terminate its subprocess and return early. Also
        marks the task ``stopped`` so the worker's write-back is
        suppressed and the next collect_notifications surfaces a
        stopped notice rather than a completed result.

        Python threads aren't killable in the hard sense, so this is a
        cooperative cancel — long-running subprocesses observe it on
        their next 100ms poll. ``timeout`` is how long we wait for the
        worker to actually exit after signalling.
        """
        with self._lock:
            task = self._tasks.get(bg_id)
            if task is None:
                return f"Error: unknown bg_id {bg_id}"
            if task.status != "running":
                return (f"[{bg_id} already {task.status}; nothing to stop]")
            task.status = "stopped"
            thread = task._thread
            cancel_event = task.cancel_event
        # Signal the subprocess to die. sandbox.execute polls this at
        # 100ms intervals; the worker thread will observe the
        # CompletedProcess and write back shortly after.
        cancel_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        return (f"[{bg_id} stop requested] cancel_event set; subprocess "
                f"terminated (best-effort). Output suppressed.")

    def list_tasks(self) -> list[dict]:
        """Snapshot of every known task (running or completed)."""
        with self._lock:
            return [{"bg_id": t.bg_id, "status": t.status,
                     "command": t.command,
                     "tool_use_id": t.tool_use_id}
                    for t in self._tasks.values()]

    def collect_notifications(self) -> list[str]:
        """Drain completed tasks and return <task_notification> blocks.

        ``stopped`` tasks drain too — the LLM needs to know its request
        was canceled rather than leaving the tool_use_id dangling.
        """
        with self._lock:
            ready = [bg_id for bg_id, t in self._tasks.items()
                     if t.status in ("completed", "stopped")]
            drained = [self._tasks.pop(bg_id) for bg_id in ready]
        out = []
        for task in drained:
            summary = task.result[:200] if task.result else ""
            out.append(
                f"<task_notification>\n"
                f"  <task_id>{task.bg_id}</task_id>\n"
                f"  <status>{task.status}</status>\n"
                f"  <command>{task.command}</command>\n"
                f"  <summary>{summary}</summary>\n"
                f"</task_notification>")
        return out
