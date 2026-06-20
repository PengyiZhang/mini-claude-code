"""AgentLoop — the core per-project, per-session agent loop.

Streams events via run() generator:
    {"type": "text", "text": str}
    {"type": "tool_use", "name": str, "input": dict, "id": str}
    {"type": "tool_result", "tool_use_id": str, "content": str}
    {"type": "done"}
    {"type": "error", "message": str}

State is persisted to project.storage after each turn so the session can
be resumed across crashes.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..config import (DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES,
                      default_config)
from ..mcp import MCPPool
from ..sandbox import Sandbox
from ..scheduler import CronScheduler
from ..skills import SkillLoader
from ..storage import Storage
from ..teams import TeammateSpawner
from ..tools import Tool, ToolContext, builtin_tools, dispatch, to_anthropic
from ..tools.background import BackgroundScheduler, should_run_background
from .compaction import prepare_context, compact_history
from .recovery import RecoveryState, is_prompt_too_long_error, with_retry
from .system_prompt import assemble_system_prompt

CONTINUATION_PROMPT = ("Continue from the previous response. "
                       "Do not repeat completed work.")


def _block_type(b) -> str | None:
    if isinstance(b, dict):
        return b.get("type")
    return getattr(b, "type", None)


def _has_tool_use(content) -> bool:
    if not isinstance(content, list):
        return False
    return any(_block_type(b) == "tool_use" for b in content)


@dataclass
class ProjectRef:
    """Minimal project handle AgentLoop needs. Avoids circular import with
    projects.manager.Project."""
    project_id: str
    project_root: str
    sandbox: Sandbox
    storage: Storage
    skills_catalog: str = ""
    skills_loader: SkillLoader | None = None
    scheduler: CronScheduler | None = None
    mcp_pool: MCPPool | None = None
    background: BackgroundScheduler | None = None
    teams: TeammateSpawner | None = None
    mcp_servers: list[str] = field(default_factory=list)
    client_factory: Callable | None = None  # override for tests


class AgentLoop:
    def __init__(self, project: ProjectRef, session_id: str,
                 tools: list[Tool] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 model: str | None = None):
        self.project = project
        self.session_id = session_id
        # If caller passes a frozen tool list, we use it as-is; otherwise we
        # rebuild each iteration so newly-connected MCP tools appear live.
        self._frozen_tools = tools
        self.tools: list[Tool] = tools if tools is not None else self._build_tools()
        self._handlers = dispatch(self.tools)
        self.on_event = on_event
        self.messages: list[dict] = project.storage.load_messages(
            project.project_id, session_id)
        self.todos: list[dict] = project.storage.load_todos(
            project.project_id, session_id)
        self.state = RecoveryState()
        if model:
            self.state.current_model = model
        self._stop = threading.Event()
        self._rounds_since_todo = 0
        self._client = None

    # ── Tool pool ───────────────────────────────────────────────────────
    def _build_tools(self) -> list[Tool]:
        """Builtin tools + any MCP tools the project currently has connected."""
        tools = builtin_tools()
        if self.project.mcp_pool is not None:
            tools = tools + self.project.mcp_pool.all_tools()
        return tools

    def _refresh_tools(self) -> None:
        """Rebuild the tool pool (called each iteration) unless caller froze it."""
        if self._frozen_tools is not None:
            return
        self.tools = self._build_tools()
        self._handlers = dispatch(self.tools)

    # ── Persistence ─────────────────────────────────────────────────────
    def _persist(self):
        self.project.storage.save_messages(
            self.project.project_id, self.session_id, self.messages)
        self.project.storage.save_todos(
            self.project.project_id, self.session_id, self.todos)

    def _emit(self, ev: dict):
        if self.on_event:
            self.on_event(ev)

    @property
    def client(self):
        if self._client is None:
            factory = self.project.client_factory or default_config().build_client
            self._client = factory()
        return self._client

    # ── Main loop ───────────────────────────────────────────────────────
    def stop(self):
        self._stop.set()

    def run(self, user_input: str | None = None) -> Iterator[dict]:
        """Run a single user turn to completion, streaming events.

        If user_input is None, the caller is responsible for having appended
        the user message already (used by the s20 shim).
        """
        if user_input is not None:
            self.messages.append({"role": "user", "content": user_input})
        max_tokens = DEFAULT_MAX_TOKENS

        while not self._stop.is_set():
            self._inject_cron_fired()
            self._inject_background_notifications()
            self._maybe_remind_todos()
            self._refresh_tools()
            prepare_context(self.messages)

            system = assemble_system_prompt(
                project_root=self.project.project_root,
                tools=self.tools,
                memories=self.project.storage.load_memory(self.project.project_id)[:2000],
                mcp_servers=(self.project.mcp_pool.list_connected()
                             if self.project.mcp_pool else self.project.mcp_servers),
                skills_catalog=self.project.skills_catalog,
            )

            try:
                response = with_retry(
                    lambda: self.client.messages.create(
                        model=self.state.current_model,
                        system=system,
                        messages=self.messages,
                        tools=to_anthropic(self.tools),
                        max_tokens=max_tokens),
                    self.state,
                    on_event=self._emit,
                )
            except Exception as e:
                if is_prompt_too_long_error(e) and not self.state.has_attempted_reactive_compact:
                    self.messages[:] = compact_history(self.messages)
                    self.state.has_attempted_reactive_compact = True
                    continue
                self.messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": f"[Error] {type(e).__name__}: {e}"}]})
                yield {"type": "error", "message": str(e)}
                self._persist()
                return

            if response.stop_reason == "max_tokens":
                if not self.state.has_escalated:
                    max_tokens = ESCALATED_MAX_TOKENS
                    self.state.has_escalated = True
                    self._emit({"type": "max_tokens_escalation",
                                "max_tokens": max_tokens})
                    continue
                self.messages.append({"role": "assistant", "content": response.content})
                if self.state.recovery_count < MAX_RECOVERY_RETRIES:
                    self.messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                    self.state.recovery_count += 1
                    continue
                yield {"type": "done"}
                self._persist()
                return

            # Normal stop_reason (end_turn / tool_use)
            max_tokens = DEFAULT_MAX_TOKENS
            self.state.has_escalated = False
            self.messages.append({"role": "assistant", "content": response.content})

            # Stream text blocks
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    yield {"type": "text", "text": block.text}

            if not _has_tool_use(response.content):
                yield {"type": "done"}
                self._persist()
                return

            results: list[dict] = []
            for ev in self._execute_tool_calls(response.content):
                yield ev
                if ev["type"] == "tool_result":
                    results.append({"type": "tool_result",
                                    "tool_use_id": ev["tool_use_id"],
                                    "content": ev["content"]})
            self.messages.append({"role": "user", "content": results})
            self._persist()

        # Stopped externally
        yield {"type": "done"}
        self._persist()

    # ── Tool execution ──────────────────────────────────────────────────
    def _maybe_remind_todos(self):
        if self._rounds_since_todo >= 3:
            self.messages.append({"role": "user",
                                  "content": "<reminder>Update your todos.</reminder>"})
            self._rounds_since_todo = 0

    def _inject_cron_fired(self) -> None:
        """Tick the project scheduler and inject any fired prompts as user
        messages so the model sees them this turn."""
        sched = self.project.scheduler
        if sched is None:
            return
        sched.tick()
        fired = sched.consume_fired()
        for job in fired:
            self.messages.append({"role": "user",
                                  "content": f"[Scheduled] {job.prompt}"})
            self._emit({"type": "cron_fired", "job_id": job.job_id,
                        "prompt": job.prompt})

    def _inject_background_notifications(self) -> None:
        bg = self.project.background
        if bg is None:
            return
        notes = bg.collect_notifications()
        if notes:
            self.messages.append({"role": "user", "content": "\n".join(notes)})
            for _ in notes:
                self._emit({"type": "background_notification"})

    def _make_ctx(self) -> ToolContext:
        def _mark():
            self.project.storage.save_todos(
                self.project.project_id, self.session_id, self.todos)
        return ToolContext(
            project_id=self.project.project_id,
            session_id=self.session_id,
            sandbox=self.project.sandbox,
            storage=self.project.storage,
            todos=self.todos,
            mark_todos_updated=_mark,
            skills_loader=self.project.skills_loader,
            scheduler=self.project.scheduler,
            mcp_pool=self.project.mcp_pool,
            teams=self.project.teams,
        )

    def _execute_tool_calls(self, content):
        """Yields tool_use + tool_result events for each tool call in content."""
        ctx = self._make_ctx()
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            tool_input = block.input or {}
            tool_use_id = block.id
            yield {"type": "tool_use", "name": name,
                   "input": tool_input, "id": tool_use_id}
            self._emit({"type": "tool_use", "name": name,
                        "input": tool_input, "id": tool_use_id})

            # Slow bash ops get offloaded; result lands as a notification
            # in a later turn.
            bg = self.project.background
            if (bg is not None and should_run_background(name, tool_input)
                    and name in self._handlers):
                bg_id = bg.start(ctx, self._handlers, name, tool_input, tool_use_id)
                output = (f"[Background task {bg_id} started] "
                          "Result will arrive as a task_notification.")
            else:
                tool = self._handlers.get(name)
                if tool is None:
                    output = f"Unknown tool: {name}"
                else:
                    output = tool.handle(ctx, tool_input)

            if name == "todo_write":
                self._rounds_since_todo = 0
            else:
                self._rounds_since_todo += 1

            yield {"type": "tool_result",
                   "tool_use_id": tool_use_id,
                   "content": output}
            self._emit({"type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": output})
