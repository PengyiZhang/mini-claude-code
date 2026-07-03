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
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..config import (DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES,
                      default_config)
from ..mcp import MCPPool
from ..sandbox import Sandbox
from ..scheduler import CronScheduler, WakeupScheduler
from ..skills import SkillLoader
from ..storage import Storage
from ..teams import TeammateSpawner
from ..tools import Tool, ToolContext, builtin_tools, dispatch, to_anthropic
from ..tools.background import BackgroundScheduler, should_run_background
from .compaction import prepare_context, compact_history
from .hooks import Hooks
from .permissions import PermissionInterceptor
from .recovery import RecoveryState, is_prompt_too_long_error, retry_delay
from .system_prompt import assemble_system_prompt, load_project_guide

CONTINUATION_PROMPT = ("Continue from the previous response. "
                       "Do not repeat completed work.")

INTERRUPTED_TOOL_RESULT = "[interrupted by server restart]"


def _chain_one(first, rest):
    """Yield ``first`` then every item from ``rest``. Used to replay the
    peeked-first event from a provider's stream back into the iteration."""
    yield first
    yield from rest


def _block_type(b) -> str | None:
    if isinstance(b, dict):
        return b.get("type")
    return getattr(b, "type", None)


def _dump_content(content):
    """Convert Anthropic SDK pydantic blocks to plain dicts so they
    survive JSON serialization. Without this, save_messages() falls
    back to default=str and we end up with "ThinkingBlock(signature=...')"
    stringified blobs on disk — destroying the structure we need to
    rehydrate the transcript after a page reload."""
    if not isinstance(content, list):
        return content
    out = []
    for b in content:
        if isinstance(b, dict):
            out.append(b)
            continue
        # pydantic v2 block (TextBlock / ToolUseBlock / ThinkingBlock / ...)
        if hasattr(b, "model_dump"):
            out.append(b.model_dump())
            continue
        # legacy pydantic v1 fallback
        if hasattr(b, "dict"):
            out.append(b.dict())
            continue
        out.append(b)
    return out


def _has_tool_use(content) -> bool:
    if not isinstance(content, list):
        return False
    return any(_block_type(b) == "tool_use" for b in content)


def _block_id(b) -> str | None:
    if isinstance(b, dict):
        return b.get("id")
    return getattr(b, "id", None)


def repair_dangling_tool_uses(messages: list[dict]) -> bool:
    """If the tail of the transcript is an assistant message with tool_use
    blocks that have no matching tool_result, append a synthetic user
    turn marking each tool as '[interrupted by server restart]'.

    Also handles the reverse direction (P1-9): if the tail is a user
    message carrying tool_result blocks whose tool_use_id does not
    match any tool_use in the preceding assistant message, those
    results are orphans — the Anthropic API rejects them with 400.
    Strip the orphans; if all results were orphans, replace the user
    message with a plain-text note so strict user/assistant alternation
    still holds.

    Used at session warm-load so a server crash mid-turn doesn't leave
    the model staring at a tool_use it can never see answered. Returns
    True if a repair happened, False otherwise. Mutates `messages` in
    place.
    """
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") == "assistant":
        return _repair_forward_dangling_tool_use(messages)
    if last.get("role") == "user":
        return _repair_reverse_orphan_tool_result(messages)
    return False


def _repair_forward_dangling_tool_use(messages: list[dict]) -> bool:
    last = messages[-1]
    content = last.get("content")
    if not isinstance(content, list):
        return False
    dangling_ids = [_block_id(b) for b in content
                    if _block_type(b) == "tool_use"]
    dangling_ids = [i for i in dangling_ids if i]
    if not dangling_ids:
        return False
    # If the next entry (there is none here since `last` is the tail)
    # would have provided results, we wouldn't be here. Append a
    # synthetic user turn with one tool_result per dangling id.
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result",
             "tool_use_id": tid,
             "content": INTERRUPTED_TOOL_RESULT,
             "is_error": True}
            for tid in dangling_ids
        ],
    })
    return True


def _repair_reverse_orphan_tool_result(messages: list[dict]) -> bool:
    """P1-9: strip user-message tool_result blocks whose tool_use_id has
    no matching tool_use in the immediately preceding assistant message.
    Such orphans make the Anthropic API return 400 on the next turn."""
    last = messages[-1]
    content = last.get("content")
    if not isinstance(content, list):
        return False
    result_blocks = [b for b in content
                     if isinstance(b, dict)
                     and b.get("type") == "tool_result"]
    if not result_blocks:
        return False

    # Collect tool_use IDs from the preceding assistant message (if any).
    valid_ids: set[str] = set()
    if len(messages) >= 2:
        prev = messages[-2]
        if prev.get("role") == "assistant":
            prev_content = prev.get("content")
            if isinstance(prev_content, list):
                for b in prev_content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        bid = b.get("id")
                        if bid:
                            valid_ids.add(bid)

    orphans = [b for b in result_blocks
               if b.get("tool_use_id") not in valid_ids]
    if not orphans:
        return False

    if len(orphans) == len(result_blocks):
        # Every tool_result is an orphan. If the user message also has
        # non-tool_result blocks (rare — text + results), keep them;
        # otherwise replace the whole message with a text note so the
        # transcript still has a valid user turn.
        non_results = [b for b in content
                       if not (isinstance(b, dict)
                               and b.get("type") == "tool_result")]
        if non_results:
            messages[-1] = {"role": "user", "content": non_results}
        else:
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": ("[Orphan tool_result blocks stripped at "
                              "warm-load — referenced tool_use ids were "
                              "missing from the prior assistant turn.]")}
                ],
            }
        return True

    # Partial: drop just the orphan result blocks, keep matched ones.
    orphan_ids = {b.get("tool_use_id") for b in orphans}
    new_content = [b for b in content
                   if not (isinstance(b, dict)
                           and b.get("type") == "tool_result"
                           and b.get("tool_use_id") in orphan_ids)]
    messages[-1] = {"role": "user", "content": new_content}
    return True


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
    memory_loader: Any = None  # mini_cc.memory.MemoryLoader
    scheduler: CronScheduler | None = None
    wakeups: "WakeupScheduler | None" = None
    mcp_pool: MCPPool | None = None
    background: BackgroundScheduler | None = None
    teams: TeammateSpawner | None = None
    lsp_manager: Any = None  # mini_cc.lsp.LSPManager
    mcp_servers: list[str] = field(default_factory=list)
    client_factory: Callable | None = None  # override for tests
    permissions: PermissionInterceptor | None = None
    prompt_tools: set[str] = field(default_factory=set)
    tenant_id: str = ""
    metrics: "object | None" = None  # MetricsRegistry or None


class AgentLoop:
    def __init__(self, project: ProjectRef, session_id: str,
                 tools: list[Tool] | None = None,
                 on_event: Callable[[dict], None] | None = None,
                 model: str | None = None,
                 system_prompt_override: str | None = None,
                 hooks: "Hooks | None" = None):
        self.project = project
        self.session_id = session_id
        # If caller passes a frozen tool list, we use it as-is; otherwise we
        # rebuild each iteration so newly-connected MCP tools appear live.
        self._frozen_tools = tools
        self.tools: list[Tool] = tools if tools is not None else self._build_tools()
        self._handlers = dispatch(self.tools)
        self.on_event = on_event
        # Phase I.B-1.3: dedicated sink for watcher-triggered (daemon-
        # path) events. ONLY routed from ``_run_until_idle`` — never
        # from ``_run_impl``'s ``_emit`` calls. This separation is what
        # prevents double-writes to events.jsonl: ``_run_impl`` yields
        # AND emits the same event for SDK callers (see the dual path
        # documented at the tool_result yield around line 1046-1056),
        # so installing the persister on ``on_event`` would duplicate
        # every tool event during a /send-driven turn. Set by
        # ``LeadWatcher._tick`` before calling ``loop.nudge``.
        self.watcher_event_sink: "Callable[[dict], None] | None" = None
        self.system_prompt_override = system_prompt_override
        self.hooks = hooks
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
        # B2: guard against two threads entering run() concurrently. The
        # SDK direct-use path doesn't always take the SessionManager
        # project lock — without this guard, two parallel run() calls
        # would race on self.messages and corrupt the transcript.
        self._running_lock = threading.RLock()
        self._running = False

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

    @staticmethod
    def _build_partial_assistant(text_parts: list[str],
                                 tool_uses: list[dict]) -> list[dict] | None:
        """P0-9 / P1-2: build a coherent assistant content list from
        whatever partial state was streamed before a cancel or exception.
        Returns None if nothing was captured (so callers can skip the
        persist). Order matches Anthropic's text-then-tool_use layout."""
        text = "".join(text_parts).strip()
        if not text and not tool_uses:
            return None
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(tool_uses)
        return blocks

    def _save_transcript(self, messages: list[dict]) -> None:
        """Pre-compaction snapshot. Writes the full message list to storage
        before any content is discarded, so callers can replay the original
        conversation later."""
        try:
            self.project.storage.write_transcript(
                self.project.project_id, messages)
        except Exception:
            # Transcript is best-effort; never block compaction on it.
            pass

    def _emit(self, ev: dict):
        if self.on_event:
            self.on_event(ev)

    def _record_usage(self, response) -> None:
        """Push Anthropic usage (input/output/cache tokens) into the
        project's MetricsRegistry, if one is attached."""
        reg = self.project.metrics
        if reg is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        try:
            from ..server.metrics import record_tokens
            record_tokens(
                reg,
                self.project.tenant_id or "unknown",
                input=getattr(usage, "input_tokens", 0) or 0,
                output=getattr(usage, "output_tokens", 0) or 0,
                cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_create=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            )
        except Exception:
            # Metrics are best-effort — never break a turn because of them.
            pass

    def _record_usage_dict(self, usage: dict) -> None:
        """Same as :meth:`_record_usage` but takes a plain dict.

        Litellm's usage shape is normalized to a dict at the provider
        boundary, so we need a dict-shaped path here.
        """
        reg = self.project.metrics
        if reg is None or not isinstance(usage, dict):
            return
        try:
            from ..server.metrics import record_tokens
            record_tokens(
                reg,
                self.project.tenant_id or "unknown",
                input=usage.get("input_tokens", 0) or 0,
                output=usage.get("output_tokens", 0) or 0,
                cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                cache_create=usage.get("cache_creation_input_tokens", 0) or 0,
            )
        except Exception:
            pass

    def _record_request_status(self, status: str) -> None:
        """Increment anthropic_request_total{status=...}."""
        reg = self.project.metrics
        if reg is None:
            return
        try:
            reg.counters["anthropic_request_total"].inc(
                tenant=self.project.tenant_id or "unknown", status=status)
        except Exception:
            pass

    @property
    def provider(self):
        """LLM provider for the current model.

        Picks anthropic vs litellm based on the model name's prefix.
        Callers can override the whole provider via ``client_factory``
        (legacy name kept for test compatibility) — useful for unit
        tests that want to swap in a stub without touching env vars.
        """
        if self._client is None:
            factory = (self.project.client_factory
                       or default_config().build_provider)
            self._client = factory()
        return self._client

    # Legacy alias — old tests reach for ``loop.client`` directly.
    client = provider

    # ── Main loop ───────────────────────────────────────────────────────
    def stop(self):
        self._stop.set()

    def set_worktree(self, path) -> None:
        """Replace the loop's sandbox with one rooted at `path`. Subsequent
        bash/read/write/edit/glob/grep calls resolve paths against the
        new root. Used by the teams subsystem to redirect a teammate's
        tool calls into a claimed task's worktree (s20 wt_ctx behavior).

        Requires the loop to own its sandbox (teammates get their own
        ProjectRef so this swap doesn't affect other sessions).
        """
        from ..sandbox import SubprocessSandbox
        self.project.sandbox = SubprocessSandbox(
            self.project.project_id, path)

    def run(self, user_input: str | None = None) -> Iterator[dict]:
        """Run a single user turn to completion, streaming events.

        If user_input is None, the caller is responsible for having appended
        the user message already (used by the s20 shim).
        """
        # B2: detect concurrent run() — the messages array isn't safe
        # against two threads appending / iterating at once. The
        # SessionManager path serializes per-project already, but SDK
        # direct-use callers can race. Fail fast with a clear error.
        with self._running_lock:
            if self._running:
                raise RuntimeError(
                    "AgentLoop.run() already in progress on this loop — "
                    "concurrent calls would corrupt the transcript. "
                    "Use separate loops for parallel sessions.")
            self._running = True

        try:
            yield from self._run_impl(user_input)
        finally:
            with self._running_lock:
                self._running = False

    def _run_until_idle(self) -> None:
        """Background-thread entry point for nudge. Drives the same
        loop body as run() but without the generator wrapper and
        without appending a fresh user_input (nudge has already
        appended the wake-up content before calling us).

        Thread-safety: the inner lock below is the real guard — if two
        callers race in, only one wins _running=True and the other
        returns immediately. nudge's pre-check under _running_lock is
        just an optimization to skip spawning a thread that would exit
        at once. Events emitted by the loop's _emit path still fire
        (so /send SSE / events.jsonl still get them), but the yielded
        events go nowhere — there is no consumer for a watcher-triggered
        turn, by design (the user isn't around)."""
        with self._running_lock:
            if self._running:
                # Lost the race with another nudge or a /send — let
                # the existing run pick up the appended content via
                # _inject_* on its next iteration.
                return
            self._running = True
        try:
            # Drain the generator so every yield runs to completion.
            # user_input=None tells _run_impl the caller already
            # appended the user message (nudge did so before calling).
            # Route every yielded event through _emit so SSE /
            # events.jsonl observers see them — there is no caller
            # iterating this generator, so without this routing the
            # watcher-triggered turn would be invisible (including
            # any {"type": "error", ...} event _run_impl yields when
            # it catches an exception internally).
            #
            # Phase I.B-1.3: ALSO route to watcher_event_sink so the
            # daemon-path events reach events.jsonl. This sink is
            # SEPARATE from on_event on purpose — _run_impl's emit/
            # yield dual path means on_event fires for events that
            # are also being yielded (and persisted by the /send
            # generator's caller). Routing the sink only from here
            # keeps /send-driven turns from double-writing.
            sink = self.watcher_event_sink
            for ev in self._run_impl(None):
                self._emit(ev)
                if sink is not None:
                    try:
                        sink(ev)
                    except Exception:
                        pass
        except Exception as e:
            # Defense-in-depth: _run_impl catches most exceptions
            # internally and yields an error event, but if something
            # escapes (e.g. a bug in _inject_*), surface it as an
            # error event so the daemon doesn't die silently.
            try:
                err_ev = {
                    "type": "error",
                    "message": f"[nudge worker] {type(e).__name__}: {e}",
                }
                self._emit(err_ev)
                sink = self.watcher_event_sink
                if sink is not None:
                    try:
                        sink(err_ev)
                    except Exception:
                        pass
            except Exception:
                pass
        finally:
            with self._running_lock:
                self._running = False

    def nudge(self, content: str) -> None:
        """Re-enter the loop with a new user message after the previous
        turn ended. Used by Phase I.B-1.2 mailbox watcher to wake lead
        when teammate milestones land while the user is away.

        - Appends ``{"role": "user", "content": content}`` to messages
        - Clears ``_stop`` so the loop can iterate again
        - If the loop is idle (no run() / _run_until_idle in flight),
          spawns a daemon thread that drains the loop until it ends
          naturally
        - If the loop is currently running (a /send is in flight),
          just appends — the running iteration's next _inject_* pass
          will pick up the new content. Does NOT start a second thread.

        Idempotent: multiple nudges queue multiple messages.
        Thread-safe: may be called from any thread. The pre-check of
        _running under _running_lock is best-effort; _run_until_idle
        re-checks under the lock and is the actual single-flight guard."""
        self._stop.clear()
        self.messages.append({"role": "user", "content": content})
        with self._running_lock:
            already_running = self._running
        if not already_running:
            t = threading.Thread(
                target=self._run_until_idle,
                name="AgentLoop-nudge",
                daemon=True,
            )
            t.start()

    def _run_impl(self, user_input: str | None = None) -> Iterator[dict]:
        if user_input is not None:
            if self.hooks is not None and self.hooks.has(Hooks.UserPromptSubmit):
                replaced = self.hooks.trigger(
                    Hooks.UserPromptSubmit, user_input)
                if isinstance(replaced, str):
                    user_input = replaced
            self.messages.append({"role": "user", "content": user_input})
            # Lead-side @mention routing: if the user typed `@name …`,
            # deliver a copy to each named teammate's inbox immediately.
            # This runs BEFORE the LLM turn so the teammate's mailbox is
            # populated by the time the lead's turn dispatches tools —
            # deterministic routing that doesn't depend on the model
            # remembering to call send_message. Only the lead session
            # routes; teammate sessions (id starts with "teammate-")
            # skip this to avoid echo loops.
            if (self.project.teams is not None
                    and not self.session_id.startswith("teammate-")):
                from ..teams.mentions import route_mentions
                try:
                    recipients = route_mentions(
                        self.project.teams.bus,
                        from_agent="lead",
                        text=user_input,
                    )
                except Exception:
                    recipients = []
                if recipients:
                    self._emit({
                        "type": "mention_routed",
                        "from": "lead",
                        "recipients": recipients,
                        "content": user_input,
                    })
        max_tokens = DEFAULT_MAX_TOKENS

        while not self._stop.is_set():
            self._inject_cron_fired()
            self._inject_background_notifications()
            self._inject_teammate_replies()
            self._maybe_remind_todos()
            self._refresh_tools()
            prepare_context(self.messages, before_compact=self._save_transcript)

            if self.system_prompt_override is not None:
                system = self.system_prompt_override
            else:
                system = assemble_system_prompt(
                    project_root=self.project.project_root,
                    tools=self.tools,
                    memories=self.project.storage.load_memory(self.project.project_id)[:2000],
                    mcp_servers=(self.project.mcp_pool.list_connected()
                                 if self.project.mcp_pool else self.project.mcp_servers),
                    skills_catalog=self.project.skills_catalog,
                    project_guide=load_project_guide(self.project.project_root),
                )

            try:
                # P1-2: initialize partial buffers BEFORE stream open
                # so an exception during the open retry loop (429/529
                # exhaustion, network) still has well-defined empty
                # state when the outer except persists the partial.
                partial_text_parts: list[str] = []
                partial_tool_uses: list[dict] = []
                # Stream normalized events from whichever provider is
                # active (Anthropic SDK or litellm). The provider yields
                # text_delta, tool_use, and a terminal message_stop
                # carrying the final assistant content + usage. Loop
                # responsibilities here are: forward text deltas in real
                # time, retry 429/529 on connection setup, react to
                # prompt-too-long errors, and extract tool blocks for
                # the tool dispatcher below.
                from ..server.tracing import log_span
                from ..config import MAX_RETRIES

                def _open_stream():
                    return self.provider.stream(
                        model=self.state.current_model,
                        system=system,
                        messages=self.messages,
                        tools=to_anthropic(self.tools),
                        max_tokens=max_tokens,
                    )

                def _is_rate_limit(e: Exception) -> bool:
                    status = getattr(e, "status_code", None) or 0
                    msg = str(e).lower()
                    return (status == 429 or "ratelimit" in msg
                            or "rate_limit" in msg)

                def _is_overloaded(e: Exception) -> bool:
                    status = getattr(e, "status_code", None) or 0
                    msg = str(e).lower()
                    return status == 529 or "overloaded" in msg

                # Retry connection setup on 429 / 529 (the SDK already
                # retries twice, this adds our backoff on top). Once the
                # stream is open, mid-stream errors propagate. The
                # iterator is lazy — opening just validates creds/rate.
                stream_iter = None
                first = None
                for attempt in range(MAX_RETRIES):
                    try:
                        stream_iter = _open_stream()
                        # Pull the first event eagerly so provider-side
                        # connection errors surface here (before we
                        # commit to streaming the whole turn).
                        first = next(stream_iter)
                        break
                    except StopIteration:
                        # Provider yielded nothing (e.g. empty completion).
                        # Keep stream_iter so we can still close it; we
                        # just have no events to forward.
                        first = None
                        break
                    except Exception as e:
                        if _is_rate_limit(e):
                            self._emit({"type": "retry", "reason": "429",
                                        "attempt": attempt + 1})
                            time.sleep(retry_delay(attempt))
                            continue
                        if _is_overloaded(e):
                            self.state.consecutive_529 += 1
                            self._emit({"type": "retry", "reason": "529",
                                        "attempt": attempt + 1})
                            time.sleep(retry_delay(attempt))
                            continue
                        raise
                else:
                    raise RuntimeError("Max retries exceeded opening stream")

                response = None  # becomes the message_stop StreamEvent
                cancelled = False
                streamed_text = False
                with log_span("anthropic.request",
                              tenant=self.project.tenant_id or "unknown",
                              model=self.state.current_model,
                              session_id=self.session_id):
                    try:
                        if first is not None:
                            events = _chain_one(first, stream_iter)
                        else:
                            events = stream_iter
                        for ev in events:
                            if self._stop.is_set():
                                self._record_request_status("cancelled")
                                cancelled = True
                                break
                            if ev.kind == "text_delta" and ev.text:
                                streamed_text = True
                                partial_text_parts.append(ev.text)
                                yield {"type": "text", "text": ev.text}
                            elif ev.kind == "tool_use":
                                # Litellm provider emits streaming tool_use
                                # events; capture the partial so cancel/error
                                # can still persist a matched-shape assistant.
                                if ev.tool_call_id:
                                    partial_tool_uses.append({
                                        "type": "tool_use",
                                        "id": ev.tool_call_id,
                                        "name": ev.tool_name or "",
                                        "input": ev.tool_input or {},
                                    })
                            elif ev.kind == "message_stop":
                                response = ev
                        if not cancelled and response is not None:
                            if response.usage:
                                self._record_usage_dict(response.usage)
                            self._record_request_status("success")
                    except Exception:
                        if cancelled:
                            pass
                        else:
                            raise
                    finally:
                        if cancelled and stream_iter is not None:
                            # Closing the generator forces any open
                            # context manager (anthropic stream / litellm
                            # HTTP response) to unwind cleanly.
                            close = getattr(stream_iter, "close", None)
                            if callable(close):
                                try:
                                    close()
                                except Exception:
                                    pass

                if cancelled:
                    # P0-9: persist the partial assistant message so the
                    # transcript reflects what the model actually generated
                    # before stop(). If a tool_use was in flight, synthesize
                    # a matching tool_result so the next turn's API call
                    # sees balanced use/result pairs.
                    partial = self._build_partial_assistant(
                        partial_text_parts, partial_tool_uses)
                    if partial is not None:
                        self.messages.append({"role": "assistant",
                                              "content": partial})
                        tool_use_ids = [b["id"] for b in partial
                                        if isinstance(b, dict)
                                        and b.get("type") == "tool_use"]
                        if tool_use_ids:
                            self.messages.append({
                                "role": "user",
                                "content": [
                                    {"type": "tool_result",
                                     "tool_use_id": tid,
                                     "content": INTERRUPTED_TOOL_RESULT,
                                     "is_error": True}
                                    for tid in tool_use_ids
                                ],
                            })
                    # P0-2: emit a `cancelled` event BEFORE `done` so SSE
                    # consumers can distinguish "turn finished cleanly"
                    # from "user clicked stop". The metric counter below
                    # already exists; this is the per-stream signal.
                    yield {"type": "cancelled"}
                    yield {"type": "done"}
                    self._persist()
                    return
            except Exception as e:
                self._record_request_status("error")
                if is_prompt_too_long_error(e) and not self.state.has_attempted_reactive_compact:
                    self.messages[:] = compact_history(
                        self.messages, before_compact=self._save_transcript)
                    self.state.has_attempted_reactive_compact = True
                    continue
                # P1-2: if partial assistant content was streamed before
                # the error, persist it as its own assistant message —
                # separate from the [Error] note below — so resume shows
                # the model the text it actually produced.
                partial = self._build_partial_assistant(
                    partial_text_parts, partial_tool_uses)
                if partial is not None:
                    self.messages.append({"role": "assistant", "content": partial})
                    tool_use_ids = [b["id"] for b in partial
                                    if isinstance(b, dict)
                                    and b.get("type") == "tool_use"]
                    if tool_use_ids:
                        self.messages.append({
                            "role": "user",
                            "content": [
                                {"type": "tool_result",
                                 "tool_use_id": tid,
                                 "content": INTERRUPTED_TOOL_RESULT,
                                 "is_error": True}
                                for tid in tool_use_ids
                            ],
                        })
                self.messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": f"[Error] {type(e).__name__}: {e}"}]})
                yield {"type": "error", "message": str(e)}
                self._persist()
                return

            # response.content_blocks is already JSON-safe (providers
            # dump pydantic blocks to dicts before emitting message_stop)
            # so we can append it directly — no _dump_content needed.
            final_blocks = response.content_blocks or []

            # Fallback for providers/mocks that don't stream text deltas:
            # emit the assistant text from the final blocks so consumers
            # (UI, subagent summary) see coherent output even when the
            # underlying transport buffered the whole response.
            if not streamed_text:
                for b in final_blocks:
                    if isinstance(b, dict) and b.get("type") == "text" \
                            and b.get("text"):
                        yield {"type": "text", "text": b["text"]}

            if response.stop_reason == "max_tokens":
                if not self.state.has_escalated:
                    max_tokens = ESCALATED_MAX_TOKENS
                    self.state.has_escalated = True
                    self._emit({"type": "max_tokens_escalation",
                                "max_tokens": max_tokens})
                    continue
                self.messages.append({"role": "assistant", "content": final_blocks})
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
            self.messages.append({"role": "assistant", "content": final_blocks})

            # Text deltas were already streamed during the provider
            # request. Fall through to tool execution if any.

            if not _has_tool_use(final_blocks):
                if self.hooks is not None:
                    self.hooks.trigger(Hooks.Stop)
                yield {"type": "done"}
                self._persist()
                return

            results: list[dict] = []
            for ev in self._execute_tool_calls(response.content_blocks):
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

    def _inject_teammate_replies(self) -> None:
        """Drain pending teammate→lead mailbox messages into loop.messages
        so the lead's LLM sees late replies in context on the next turn.

        Without this, a reply that lands while the lead is idle
        (between /send calls, or mid-turn after the lead's check_inbox
        already returned empty) sits in the mailbox drainable only by
        an explicit check_inbox call — which the model has no reason
        to retry. Mirrors the teammate subsystem's own _idle_poll,
        which similarly drains a teammate's mailbox into the next
        turn's prompt. Skipped for teammate sessions so a teammate's
        loop never steals lead's mail."""
        teams = self.project.teams
        if teams is None or self.session_id.startswith("teammate-"):
            return
        try:
            pending = teams.bus.read_inbox("lead")
        except Exception:
            return
        if not pending:
            return
        import json as _json
        note = ("<teammate_messages>"
                + _json.dumps(pending, ensure_ascii=False)
                + "</teammate_messages>")
        self.messages.append({"role": "user", "content": note})

    def _make_ctx(self) -> ToolContext:
        def _mark():
            self.project.storage.save_todos(
                self.project.project_id, self.session_id, self.todos)
            self._emit({"type": "todos_updated", "todos": self.todos})

        # workflow_dispatch: run a prompt as a focused sub-agent against
        # the same project. Workflow tools use this to step through a
        # declared plan without the parent loop recursing into itself.
        def _workflow_dispatch(prompt: str) -> str:
            from ..core.subagent import spawn_subagent
            return spawn_subagent(
                self.project, prompt,
                on_event=self._emit,
            )

        ctx = ToolContext(
            project_id=self.project.project_id,
            session_id=self.session_id,
            sandbox=self.project.sandbox,
            storage=self.project.storage,
            todos=self.todos,
            mark_todos_updated=_mark,
            skills_loader=self.project.skills_loader,
            memory_loader=self.project.memory_loader,
            scheduler=self.project.scheduler,
            mcp_pool=self.project.mcp_pool,
            teams=self.project.teams,
            project_ref=self.project,
            background_scheduler=self.project.background,
            background_tools=self._handlers,
        )
        # ToolContext is a dataclass without slots — safe to attach.
        ctx.workflow_dispatch = _workflow_dispatch  # type: ignore[attr-defined]
        return ctx

    def _execute_tool_calls(self, content):
        """Yields tool_use + tool_result events for each tool call in content.

        ``content`` is a list of plain dicts (both providers dump to
        dicts before yielding message_stop) — use ``.get()`` rather
        than attribute access so the same code path works regardless
        of which provider produced the blocks.

        For delegating tools (currently ``task``), events emitted by the
        nested sub-AgentLoop during the tool call are collected on
        ``ctx.on_subagent_event`` and re-yielded into the parent stream
        after the tool returns, so users see subagent tool_use /
        tool_result activities land live under the parent's current
        assistant bubble rather than vanishing into a hidden transcript.
        """
        ctx = self._make_ctx()
        for block in content:
            btype = block.get("type") if isinstance(block, dict) \
                else getattr(block, "type", None)
            if btype != "tool_use":
                continue
            if isinstance(block, dict):
                name = block.get("name")
                tool_input = block.get("input") or {}
                tool_use_id = block.get("id")
            else:
                name = block.name
                tool_input = block.input or {}
                tool_use_id = block.id
            yield {"type": "tool_use", "name": name,
                   "input": tool_input, "id": tool_use_id}
            self._emit({"type": "tool_use", "name": name,
                        "input": tool_input, "id": tool_use_id})

            # Per-call sink for nested-loop events. Re-bound every
            # iteration so each tool call gets a fresh buffer; the
            # closure captures the current list by reference.
            subagent_events: list[dict] = []
            ctx.on_subagent_event = lambda ev, sink=subagent_events: sink.append(ev)

            # Interactive permission prompt: if the project opted in to
            # per-tool prompts (via .mini_cc/permissions.toml), ask the
            # client before running. Yields a permission_request event,
            # then blocks on the interceptor's Event until a decision
            # arrives via HTTP, the timeout elapses, or the session
            # stops.
            interceptor = self.project.permissions
            if (interceptor is not None
                    and name in self.project.prompt_tools):
                req = interceptor.create(
                    self.session_id, name, tool_input)
                yield {"type": "permission_request",
                       "request_id": req.request_id,
                       "tool_name": name, "tool_input": tool_input,
                       "id": tool_use_id}
                self._emit({"type": "permission_request",
                            "request_id": req.request_id,
                            "tool_name": name, "tool_input": tool_input,
                            "id": tool_use_id})
                decided = interceptor.wait(
                    req.request_id, stop_event=self._stop)
                if decided is None or decided.decision != "allow":
                    output = (decided.deny_message if decided
                              and decided.deny_message
                              else "[permission timed out]")
                    yield {"type": "tool_result",
                           "tool_use_id": tool_use_id,
                           "content": output}
                    self._emit({"type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": output})
                    if name == "todo_write":
                        self._rounds_since_todo = 0
                    else:
                        self._rounds_since_todo += 1
                    continue
                # Decision = allow → fall through to normal execution.

            # PreToolUse hook can deny the call; the denial string
            # becomes the tool_result content.
            denied = (self.hooks.trigger(Hooks.PreToolUse, name, tool_input)
                      if self.hooks is not None else None)
            if denied is not None:
                output = str(denied)
            else:
                # Slow bash ops get offloaded; result lands as a
                # notification in a later turn.
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
                if self.hooks is not None:
                    self.hooks.trigger(Hooks.PostToolUse, name, tool_input, output)

            if name == "todo_write":
                self._rounds_since_todo = 0
            else:
                self._rounds_since_todo += 1

            # Drain any events the nested sub-AgentLoop emitted during
            # tool.handle() (e.g. the `task` tool). We yield them BEFORE
            # the wrapping tool_result so the frontend renders the
            # subagent's tool_use / tool_result activities as siblings
            # of the parent's `task` activity within the same assistant
            # bubble. Ordering within subagent_events is preserved
            # because _emit fires synchronously inside tool.handle().
            for sub_ev in subagent_events:
                yield sub_ev
                self._emit(sub_ev)
            subagent_events.clear()
            ctx.on_subagent_event = None

            yield {"type": "tool_result",
                   "tool_use_id": tool_use_id,
                   "content": output}
            self._emit({"type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": output})

            # todo_write ran — yield the new list so HTTP/SSE clients can
            # update the task board live. _emit also fires for SDK callers
            # who wired on_event, but yield is what reaches the SSE stream
            # (SessionManager.send doesn't propagate on_event).
            if name == "todo_write":
                yield {"type": "todos_updated", "todos": self.todos}
