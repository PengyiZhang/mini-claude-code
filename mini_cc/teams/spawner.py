"""Per-project teammate registry — daemon-thread sub-AgentLoop runner."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .bus import MessageBus, _format_inbox_as_dialogue
from .convention import convention_prompt
from .protocol import ProtocolState, ProtocolTracker

if TYPE_CHECKING:
    from ..core.loop import AgentLoop
    from ..storage import Storage, Task


@dataclass
class TeammateInfo:
    name: str
    role: str
    alive: bool = True
    thread: threading.Thread | None = None
    started_at: float = field(default_factory=time.time)
    stopped_at: float = 0.0
    # 手工 spawn 的 teammate 默认常驻 (debug.7.md Task 1) — 仅在用户
    # 显式 persistent=False 时才会在 idle_timeout 后退出。
    persistent: bool = True
    # 最近一次使用的 prompt — 用于 /agents edit 调整后下次 idle 唤醒时
    # 注入。当前 _runner 直接通过 inbox 内容驱动下一轮，prompt 仅作为
    # 元数据保留供 /agents roster 显示与未来 re-spawn。
    prompt: str = ""
    # Live event sink — re-bindable across turns. _runner reads this on
    # every emitted event so the lead can refresh the sink when a new
    # tool call wires up a fresh ctx.on_subagent_event (debug.7.md Task
    # 1c: @mention on later turns must still surface teammate events
    # to the main session).
    event_sink: Callable[[dict], None] | None = None
    # Park-after-result: set True after the teammate sends a
    # msg_type="result" (signalling its whole mission is complete).
    # While parked, _idle_poll only wakes on shutdown_request / a lead
    # @mention (new task) / an unclaimed task — it ignores chatter
    # (lead acks, peer chitchat) so a finished teammate stops burning
    # LLM turns. Cleared when genuine new work wakes it.
    parked: bool = False


class TeammateSpawner:
    """Per-project registry of running teammates.

    Each teammate runs a sub-AgentLoop in a daemon thread. The spawner:
    - forwards streamed events to an optional on_event callback
    - between turns, observes the teammate's inbox for shutdown_request
      and plan_approval_response
    - between turns, runs _idle_poll to wake on inbox messages or
      unclaimed tasks (auto-claimed and injected as the next prompt)
    - enforces the plan-approval gate by blocking the next turn after
      submit_plan until review_plan arrives
    """

    def __init__(self, workspace: Path,
                 loop_factory: Callable[[str], "AgentLoop"], *,
                 project_id: str | None = None,
                 storage: "Storage | None" = None,
                 idle_poll_interval: float = 5.0,
                 idle_timeout: float = 60.0,
                 plan_approval_timeout: float = 600.0):
        self.bus = MessageBus(workspace)
        # Per-message read/ignored state for the TeammatesPanel history
        # view. Sidecar JSON; survives inbox drain and process restart.
        from .disposition import InboxDisposition
        self.disposition = InboxDisposition(self.bus.dir)
        self.protocol = ProtocolTracker()
        self._loop_factory = loop_factory
        self.project_id = project_id
        self.storage = storage
        self.idle_poll_interval = idle_poll_interval
        self.idle_timeout = idle_timeout
        # B5: cap for _wait_for_plan_verdict. Default 10 min — long
        # enough for a human review during work hours, short enough
        # that a dead lead doesn't lock the teammate forever.
        self.plan_approval_timeout = plan_approval_timeout
        self._lock = threading.Lock()
        self._teammates: dict[str, TeammateInfo] = {}
        # name -> request_id the teammate is currently blocked on
        self._waiting_plan: dict[str, str] = {}
        # debug.8 Task A: side-channel queue for teammate→lead messages.
        # The lead's HTTP /send route drains this between loop events so
        # the lead's SSE stream surfaces teammate activity in real time
        # even after the spawn-time sink goes stale. Pre-fix, late
        # teammate sends were dropped silently — the user saw a frozen
        # UI while lead.jsonl filled up off-screen.
        import collections
        self._lead_events: "collections.deque[dict]" = collections.deque()
        # Bug 3: lead session binding. When Alice sends a reply while
        # the lead is NOT in an active /send SSE stream, the
        # teammate_message event queued in _lead_events was previously
        # lost on the next refresh — the /send drain path is the only
        # caller of append_session_event. Recording the lead's active
        # sid here lets _emit_to_lead persist the event directly so
        # hydrate can replay it even if /send never runs again.
        self._lead_session_id: str | None = None
        # Hook fired on every bus.send when to_agent == "lead". Lets us
        # push the message into _lead_events without polling lead.jsonl.
        self.bus.set_lead_hook(self._emit_to_lead)
        # Phase I.B-1.2: LeadWatcher daemon — null until explicitly
        # started via start_lead_watcher. Tests / legacy callers that
        # don't care about auto-wake keep this None forever.
        self._lead_watcher: "LeadWatcher | None" = None

    # ── Lead watcher (Phase I.B-1.2) ──────────────────────────────────
    def start_lead_watcher(
        self,
        lead_loop_getter: "Callable[[], AgentLoop | None]",
        *,
        poll_interval: float = 1.0,
        debounce: float = 5.0,
        event_persister: "Callable[[dict], None] | None" = None,
    ) -> None:
        """Construct and start the LeadWatcher daemon bound to this
        spawner's bus + project_id.

        The watcher polls lead's mailbox and nudges the lead loop when
        teammate result/milestone/blocker messages land while the user
        is away. See ``mini_cc.teams.watcher.LeadWatcher`` for the full
        threading model.

        ``event_persister`` is a callback the watcher installs on the
        lead loop's ``watcher_event_sink`` so daemon-path events reach
        events.jsonl — closes the user-away observability gap (Task
        I.B-1.3). The /send route passes a closure over
        ``storage.append_session_event``.

        Re-binds on every call: if a watcher is already running, its
        ``lead_loop_getter`` and ``event_persister`` are swapped in
        place. Without this, the FIRST /send's closures would win
        forever — opening session B would still persist daemon events
        into session A's events.jsonl.

        Not wired into ``__init__`` deliberately: existing tests build
        spawners without expecting a background thread, and the server
        routes opt in via an explicit call (Task I.B-1.3).
        """
        from .watcher import LeadWatcher
        with self._lock:
            existing = self._lead_watcher
            if existing is not None and existing.is_alive():
                # Re-bind so the new /send's session gets the events.
                existing.rebind(lead_loop_getter, event_persister)
                return
            watcher = LeadWatcher(
                bus=self.bus,
                project_id=self.project_id or "unknown",
                lead_loop_getter=lead_loop_getter,
                poll_interval=poll_interval,
                debounce=debounce,
                event_persister=event_persister,
            )
            self._lead_watcher = watcher
        # Start outside the lock — LeadWatcher.start() spawns the
        # thread and returns; we don't want to hold the spawner lock
        # across thread-spawn boundaries.
        watcher.start()

    def stop_lead_watcher(self, *, join_timeout: float = 5.0) -> None:
        """Stop the LeadWatcher daemon if one is running. Safe to call
        when no watcher was started (no-op)."""
        with self._lock:
            watcher = self._lead_watcher
            self._lead_watcher = None
        if watcher is not None:
            watcher.stop(join_timeout=join_timeout)

    def set_lead_session(self, sid: str | None) -> None:
        """Record (or clear) the lead's active session id.

        Called by the /send route at start of turn so subsequent
        teammate→lead messages can be persisted directly into that
        session's transcript — the message survives a page refresh
        even when no /send is running to drain the side-channel.

        Switching to a DIFFERENT session clears the side-channel
        deque: events still pending from a previous session's
        lifetime belong to that session (they were already
        persisted at emit time), not this new one. Pre-fix, a new
        /send drained stale events from a previous lead session
        into the new session's live SSE stream — the persisted
        transcript stayed clean (emit-time persistence is
        session-scoped), but the live UI showed contaminated output
        until refresh (debug.9.md).
        """
        if sid != self._lead_session_id:
            self._lead_events.clear()
        self._lead_session_id = sid

    # ── Public API (used by lead tools) ────────────────────────────────
    def list_alive(self) -> list[TeammateInfo]:
        with self._lock:
            return [t for t in self._teammates.values() if t.alive]

    def spawn(self, name: str, role: str, prompt: str,
              on_event=None, *, persistent: bool = True) -> str | None:
        with self._lock:
            existing = self._teammates.get(name)
            if existing is not None and existing.alive:
                return f"Teammate '{name}' already exists"
            self._prune_stopped_locked()
        info = TeammateInfo(name=name, role=role, persistent=persistent,
                            prompt=prompt, event_sink=on_event)
        thread = threading.Thread(
            target=self._runner, args=(info, prompt),
            daemon=True, name=f"teammate:{name}")
        info.thread = thread
        with self._lock:
            self._teammates[name] = info
        thread.start()
        return None

    def bind_event_sink(self, name: str, sink) -> str | None:
        """Re-bind the teammate's live event sink (debug.7.md Task 1c).

        Lead calls this whenever a fresh ctx.on_subagent_event is set up
        (per tool call) so subsequent idle-wake turns route events into
        the current SSE stream instead of the original spawn-time sink
        which becomes stale once the spawn_teammate call returns.

        Pass None to clear. Returns None on success or an error string.
        """
        with self._lock:
            info = self._teammates.get(name)
            if info is None:
                return f"Teammate '{name}' not found"
            info.event_sink = sink
        return None

    # Cap on retained stopped entries so `/agents` can still show
    # "recently stopped" without the registry growing unbounded across
    # many spawn/stop cycles.
    _KEEP_STOPPED = 3

    def _prune_stopped_locked(self) -> None:
        """Caller holds self._lock. Drop oldest stopped entries beyond
        the retention cap. Alive entries are always kept."""
        stopped = [t for t in self._teammates.values() if not t.alive]
        if len(stopped) <= self._KEEP_STOPPED:
            return
        stopped.sort(key=lambda t: t.stopped_at or t.started_at)
        for t in stopped[:-self._KEEP_STOPPED]:
            self._teammates.pop(t.name, None)

    def request_shutdown(self, name: str) -> str:
        with self._lock:
            info = self._teammates.get(name)
        if info is None or not info.alive:
            return f"Teammate '{name}' not found"
        req_id = self.protocol.new_request_id()
        self.protocol.register(ProtocolState(
            request_id=req_id, type="shutdown",
            sender="lead", target=name, status="pending", payload=""))
        self.bus.send("lead", name, "Shut down.", "shutdown_request",
                      {"request_id": req_id})
        return f"Shutdown request sent to {name}"

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Best-effort graceful shutdown of all teammates.

        For each alive teammate: send a shutdown_request via the bus
        (same protocol as request_shutdown), wait up to ``timeout``
        for the thread to exit naturally. Threads that don't exit in
        time are abandoned (they're daemon threads, so process exit
        will reap them — but mailbox writes mid-flight may be lost).

        Wire this into FastAPI lifespan shutdown so SIGTERM doesn't
        leave teammates mid-write.
        """
        with self._lock:
            infos = list(self._teammates.values())
        for info in infos:
            if not info.alive:
                continue
            try:
                self.request_shutdown(info.name)
            except Exception:
                pass
        deadline = time.monotonic() + timeout
        for info in infos:
            if info.thread is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if info.thread.is_alive():
                info.thread.join(timeout=remaining)

    def submit_plan(self, teammate_name: str, plan: str) -> str:
        """Teammate-side: register a pending plan, notify lead, block."""
        req_id = self.protocol.new_request_id()
        self.protocol.register(ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=teammate_name, target="lead",
            status="pending", payload=plan))
        with self._lock:
            self._waiting_plan[teammate_name] = req_id
        self.bus.send(teammate_name, "lead", plan,
                      "plan_approval_request", {"request_id": req_id})
        return req_id

    def review_plan(self, request_id: str, approve: bool,
                    feedback: str = "") -> str:
        state = self.protocol.get(request_id)
        if state is None or state.type != "plan_approval":
            return f"Request {request_id} not found"
        self.protocol.update_status(
            request_id, "approved" if approve else "rejected")
        self.bus.send("lead", state.sender,
                      feedback or ("Approved" if approve else "Rejected"),
                      "plan_approval_response",
                      {"request_id": request_id, "approve": approve})
        return f"Plan {'approved' if approve else 'rejected'}"

    def request_plan(self, teammate_name: str, task: str) -> str:
        with self._lock:
            info = self._teammates.get(teammate_name)
        if info is None or not info.alive:
            return f"Teammate '{teammate_name}' not found"
        self.bus.send("lead", teammate_name,
                      f"Submit plan for: {task}", "message")
        return f"Asked {teammate_name} to submit a plan"

    def delete(self, name: str) -> str:
        """Remove a stopped teammate from the registry (debug.7.md Task 1d).

        Refuses alive teammates — the caller must ``request_shutdown`` and
        wait for the worker thread to exit first. Force-removing an alive
        entry would leave the thread running with no registry handle.
        Returns None on success or an error string.
        """
        with self._lock:
            info = self._teammates.get(name)
            if info is None:
                return f"Teammate '{name}' not found"
            if info.alive:
                return (f"Teammate '{name}' is still alive — "
                        "stop it first with `/agents stop`")
            self._teammates.pop(name, None)
        return None

    def edit(self, name: str, *, role: str | None = None,
             prompt: str | None = None) -> str:
        """Update role and/or prompt on a stopped teammate (debug.7.md Task 1d).

        Editing a live teammate mid-flight is refused — the running loop
        has already captured the original values; subsequent idle turns
        are driven by inbox contents, not the stored prompt.
        Returns None on success or an error string.
        """
        with self._lock:
            info = self._teammates.get(name)
            if info is None:
                return f"Teammate '{name}' not found"
            if info.alive:
                return (f"Teammate '{name}' is still alive — "
                        "stop it before editing")
            if role is not None:
                info.role = role
            if prompt is not None:
                info.prompt = prompt
        return None

    def list_pending_requests(self) -> list[ProtocolState]:
        return self.protocol.list_pending()

    # ── Lead side-channel (debug.8 Task A) ─────────────────────────────
    def _emit_to_lead(self, msg: dict) -> None:
        """Push a teammate→lead message onto the side-channel queue as a
        synthetic ``teammate_message`` event. The lead's HTTP /send route
        drains this between loop events so the SSE stream shows the
        teammate's reply in real time, even when the original
        spawn-time sink has gone stale.

        Bug 3: also persist the event directly into the lead's bound
        session transcript so a page refresh replays it. Pre-fix the
        event was only persisted when /send happened to be running to
        drain it — if the lead was idle, the message queued in
        _lead_events was orphaned and hydrate lost it on the next
        refresh. Persistence is best-effort: a missing binding or
        storage error must never block the live path.

        The event shape mirrors the bus message plus ``type`` so the
        frontend SSE switch can dispatch on it like any other event."""
        ev = {
            "type": "teammate_message",
            "from": msg.get("from"),
            "to": msg.get("to"),
            "content": msg.get("content", ""),
            "msg_type": msg.get("type", "message"),
            "ts": msg.get("ts", time.time()),
            "metadata": msg.get("metadata") or {},
        }
        self._lead_events.append(ev)
        sid = self._lead_session_id
        if sid and self.project_id and self.storage is not None:
            try:
                seq = self.storage.append_session_event(self.project_id, sid, ev)
                # debug.10: tag the in-memory event with its real event-log
                # seq so the /send SSE path can yield ``(seq, ev)`` tuples —
                # this lets the frontend dedup against the parallel
                # ``GET /sessions/{sid}/events`` tail stream when both
                # deliver the same record.
                ev["_seq"] = seq
            except Exception:
                # Best-effort: a storage failure must not block the
                # live side-channel. The message is still in
                # _lead_events for an active /send to drain.
                pass

    def drain_lead_events(self) -> list[dict]:
        """Pop every queued teammate→lead event. Called by the lead's
        send route between loop iterations. Returns in insertion order."""
        if not self._lead_events:
            return []
        out = list(self._lead_events)
        self._lead_events.clear()
        return out

    # ── Runner (worker thread body) ────────────────────────────────────
    def _runner(self, info: TeammateInfo, prompt: str):
        # Hyphen not colon: see core/subagent.py for the validate_id rationale.
        loop = self._loop_factory(f"teammate-{info.name}")
        identity = (f"<identity>You are '{info.name}', a {info.role}. "
                    f"Work with tools, with your peers, and with the "
                    f"lead to complete the mission below.</identity>")
        # Convention is prepended (before identity+prompt) so the model
        # treats it as ground truth for protocol behavior. Prompt authors
        # only need to specify WHAT to do, not re-state protocol rules.
        convention = convention_prompt()

        def _forward(ev: dict) -> None:
            """Live event forwarding — re-read info.event_sink each call
            so bind_event_sink takes effect on the next event."""
            with self._lock:
                sink = info.event_sink
            if sink is not None:
                try:
                    sink(ev)
                except Exception:
                    # A broken sink must never tear down the teammate.
                    pass

        try:
            # Pre-spawn drain: handle any pending protocol messages.
            for msg in self.bus.read_inbox(info.name):
                if msg.get("type") == "shutdown_request":
                    self._send_shutdown_response(info.name, msg)
                    return

            next_input = f"{convention}\n\n{identity}\n\n{prompt}"
            should_shutdown = False

            # Clear any stale result flag for this name (e.g. a
            # finally-block "Done." result from a previous tenant of the
            # same name) so we don't park before the first real turn.
            self.bus.take_result_flag(info.name)

            while not should_shutdown:
                # Run one full turn (model + tool calls). Mid-turn
                # shutdown detection lets a long-running generator exit
                # as soon as the lead requests shutdown.
                for ev in loop.run(next_input):
                    _forward(ev)
                    if ev.get("type") == "done":
                        pending = self.bus.peek_inbox(info.name)
                        if any(m.get("type") == "shutdown_request"
                               for m in pending):
                            self.bus.read_inbox(info.name)
                            sh = next(m for m in pending
                                      if m.get("type") == "shutdown_request")
                            self._send_shutdown_response(info.name, sh)
                            should_shutdown = True
                            break

                if should_shutdown:
                    break

                # Park-after-result: if the teammate sent a result during
                # that turn, its mission is complete — mark it parked so
                # the idle poll below only wakes it for genuine new work
                # (shutdown / lead @mention / unclaimed task), not for
                # the lead's acknowledgement chatter that would otherwise
                # ping-pong another full LLM turn.
                if self.bus.take_result_flag(info.name):
                    info.parked = True

                # Plan-approval gate: block until the response arrives.
                with self._lock:
                    waiting_req = self._waiting_plan.get(info.name)
                if waiting_req:
                    verdict = self._wait_for_plan_verdict(info, waiting_req)
                    if verdict is None:
                        should_shutdown = True
                        break
                    next_input = verdict
                    continue

                # Idle poll for inbox messages or unclaimed tasks.
                # Persistent teammates must NOT re-enter loop.run() on
                # timeout — that re-runs the LLM with stale next_input
                # every idle_timeout cycle (one burn per minute forever;
                # observed 25+ identical "Work Complete Summary" writes
                # to lead.jsonl during Playwright e2e). Instead, keep
                # re-polling the inbox until real work arrives or a
                # shutdown turns up. Only non-persistent teammates fall
                # through to the exit on first timeout.
                if info.persistent:
                    while True:
                        result, user_input = self._idle_poll(info, loop)
                        if result == "shutdown":
                            should_shutdown = True
                            break
                        if result == "work":
                            next_input = user_input or ""
                            break
                        # timeout: keep polling without LLM call
                    if should_shutdown:
                        break
                    continue
                result, user_input = self._idle_poll(info, loop)
                if result == "shutdown":
                    should_shutdown = True
                    break
                if result == "timeout":
                    break
                next_input = user_input or ""
        except Exception as e:
            self.bus.send(info.name, "lead",
                          f"[error] {type(e).__name__}: {e}", "result")
        finally:
            with self._lock:
                if info.name in self._teammates:
                    info = self._teammates[info.name]
                    info.alive = False
                    info.stopped_at = time.time()
                self._prune_stopped_locked()
            self.bus.send(info.name, "lead", "Done.", "result")

    def _wait_for_plan_verdict(self, info: TeammateInfo,
                               request_id: str,
                               *,
                               timeout: float | None = None) -> str | None:
        """Block until plan_approval_response arrives. Returns the verdict
        prompt, or None if a shutdown_request was seen instead.

        B5: ``timeout`` (seconds) caps how long we wait. If the lead
        never responds, the teammate exits rather than polling forever.
        Defaults to ``self.plan_approval_timeout`` (10 min) when None.
        """
        if timeout is None:
            timeout = getattr(self, "plan_approval_timeout", 600.0)
        deadline = time.time() + timeout
        while True:
            if time.time() >= deadline:
                with self._lock:
                    self._waiting_plan.pop(info.name, None)
                # Mark the plan request as expired so the lead's later
                # review doesn't find a live teammate to deliver to.
                try:
                    self.protocol.update_status(request_id, "expired")
                except Exception:
                    pass
                return ("[Plan approval timed out after "
                        f"{int(timeout)}s — exiting turn]")
            time.sleep(self.idle_poll_interval)
            inbox = self.bus.read_inbox(info.name)
            for msg in inbox:
                mtype = msg.get("type")
                if mtype == "shutdown_request":
                    self._send_shutdown_response(info.name, msg)
                    return None
                if mtype == "plan_approval_response":
                    meta = msg.get("metadata", {})
                    if meta.get("request_id") == request_id:
                        with self._lock:
                            self._waiting_plan.pop(info.name, None)
                        approve = meta.get("approve", False)
                        return ("[Plan approved]" if approve
                                else f"[Plan rejected] {msg.get('content', '')}")

    def _idle_poll(self, info: TeammateInfo,
                   loop: "AgentLoop | None" = None
                   ) -> tuple[str, str | None]:
        """Wait up to idle_timeout for work. Returns
        ("shutdown", None) | ("work", user_input) | ("timeout", None).

        If `loop` is provided and an auto-claimed task has a worktree
        binding, the loop is redirected into the worktree path via
        set_worktree (s20 wt_ctx behavior) before returning.

        Park-after-result: when ``info.parked`` is set (the teammate just
        sent ``msg_type="result"``), only wake for genuine new work — a
        ``shutdown_request``, a lead ``mention`` (new task), or an
        unclaimed task. Non-actionable chatter (lead acknowledgements,
        peer chitchat) is left buffered via peek (no drain) so a finished
        teammate stops burning LLM turns instead of ping-ponging acks.
        """
        deadline = time.time() + self.idle_timeout
        while time.time() < deadline:
            time.sleep(self.idle_poll_interval)
            # Wakeup scheduler: a scheduled self-paced reminder (e.g.
            # "re-check alice's reply in 30s") fires here between turns
            # — including while parked. Treat it as genuine work: clear
            # parked and run a turn with the wakeup prompt. Defensive
            # getattr chain: test FakeLoops (and any loop without a
            # project ref) must not crash _idle_poll here.
            if loop is not None:
                project = getattr(loop, "project", None)
                ws = getattr(project, "wakeups", None) if project is not None else None
                if ws is not None and hasattr(ws, "tick"):
                    fired = ws.tick()
                    if fired:
                        info.parked = False
                        prompts = "; ".join(w.prompt for w in fired)
                        return ("work", f"<wakeup>{prompts}</wakeup>")
            if info.parked:
                # Peek (don't drain) so buffered chatter is preserved as
                # context for whenever we do wake.
                pending = self.bus.peek_inbox(info.name)
                actionable = next(
                    (m for m in pending
                     if m.get("type") in ("shutdown_request", "mention")),
                    None,
                )
                if actionable is not None:
                    inbox = self.bus.read_inbox(info.name)
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._send_shutdown_response(info.name, msg)
                            return ("shutdown", None)
                    info.parked = False
                    return ("work", _format_inbox_as_dialogue(inbox, info.name))
                task = self._scan_unclaimed_tasks()
                if task is not None:
                    claimed = self._claim_as_work(info, task, loop)
                    if claimed is not None:
                        return claimed
                # Nothing actionable — stay parked, keep sleeping.
                continue
            inbox = self.bus.read_inbox(info.name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._send_shutdown_response(info.name, msg)
                        return ("shutdown", None)
                # Non-protocol (or other) messages: inject as next turn.
                # Format as natural conversation rather than raw JSON:
                # the LLM can still parse `<inbox>…</inbox>` (the tag is
                # preserved) but the chat bubble renders readable text
                # instead of an opaque JSON dump. JSON is appended at the
                # end as a fallback for any code path that still wants
                # the structured form.
                return ("work", _format_inbox_as_dialogue(inbox, info.name))
            task = self._scan_unclaimed_tasks()
            if task is not None:
                claimed = self._claim_as_work(info, task, loop)
                if claimed is not None:
                    return claimed
        return ("timeout", None)

    def _claim_as_work(
        self, info: TeammateInfo, task: "Task", loop: "AgentLoop | None",
    ) -> tuple[str, str] | None:
        """Atomically claim ``task`` for ``info.name`` and return a
        ("work", prompt) tuple for the next turn, or None if the claim
        lost a race (task no longer pending / already owned). Clears
        ``info.parked`` (claiming work reactivates a parked teammate)
        and redirects the loop sandbox into the task's worktree if bound.
        """
        claim_out = self._claim_task(task.id, info.name)
        if "Claimed" not in claim_out:
            return None
        info.parked = False
        wt_info = ""
        if task.worktree:
            wt_path = (Path(self.bus.workspace) / ".worktrees" / task.worktree)
            wt_info = f"\nWork directory: {wt_path}"
            if loop is not None and hasattr(loop, "set_worktree"):
                loop.set_worktree(wt_path)
        return ("work",
                f"<auto-claimed>Task {task.id}: "
                f"{task.subject}{wt_info}</auto-claimed>")

    def _scan_unclaimed_tasks(self) -> "Task | None":
        if self.storage is None or self.project_id is None:
            return None
        tasks = self.storage.load_tasks(self.project_id)
        by_id = {t.id: t for t in tasks}
        for t in tasks:
            if t.status != "pending" or t.owner:
                continue
            ok = True
            for dep_id in t.blockedBy:
                dep = by_id.get(dep_id)
                if dep is None or dep.status != "completed":
                    ok = False
                    break
            if ok:
                return t
        return None

    def _claim_task(self, task_id: str, owner: str) -> str:
        if self.storage is None or self.project_id is None:
            return "Storage not configured"
        # B4: use atomic CAS so parallel idle teammates can't both
        # claim the same task. Falls back to per-task load/save if the
        # storage backend doesn't expose claim_task_atomic (e.g. a
        # custom Storage implementation that hasn't been updated).
        claim = getattr(self.storage, "claim_task_atomic", None)
        if callable(claim):
            ok, msg = claim(self.project_id, task_id, owner)
            return msg
        # Legacy path (race-prone but functionally equivalent for
        # single-process deployments).
        for t in self.storage.load_tasks(self.project_id):
            if t.id == task_id:
                if t.status != "pending":
                    return f"Task {task_id} is {t.status}, cannot claim"
                if t.owner:
                    return f"Task {task_id} already owned by {t.owner}"
                t.owner = owner
                t.status = "in_progress"
                self.storage.save_task(self.project_id, t)
                return f"Claimed {t.id} ({t.subject})"
        return f"Error: task {task_id} not found"

    def _send_shutdown_response(self, name: str, msg: dict) -> None:
        req_id = msg.get("metadata", {}).get("request_id", "")
        self.bus.send(name, "lead", "Shutting down.", "shutdown_response",
                      {"request_id": req_id, "approve": True})
