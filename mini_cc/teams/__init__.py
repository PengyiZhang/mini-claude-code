"""Teams subsystem — per-project message bus, protocol tracker, spawner.

Ports s20 lines 491-872:
- MessageBus: per-project JSONL mailboxes (not module-global)
- ProtocolTracker: pending plan-approval / shutdown requests keyed by id
- TeammateSpawner: per-project registry; runs each teammate in a daemon
  thread with a sub-AgentLoop. Implements the plan-approval gate at the
  turn boundary and the autonomous idle poll with auto-claim.

Behaviour deltas vs s20 (documented simplifications):
- Plan-approval gate is enforced between turns, not mid-turn. The
  submit_plan tool tells the model to end its turn; the spawner blocks
  the next turn until review_plan arrives.
- Auto-cwd into a claimed task's worktree is implemented via
  AgentLoop.set_worktree: after a successful claim, the spawner swaps
  the teammate's sandbox root to the worktree path. The teammate gets
  its own sandbox (built in projects.manager._build_teammate_loop) so
  the swap doesn't affect other sessions.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

if TYPE_CHECKING:
    from ..core.loop import AgentLoop
    from ..storage import Storage, Task

try:
    import portalocker  # type: ignore
    _PORTALOCKER_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only when dep missing
    portalocker = None  # type: ignore
    _PORTALOCKER_AVAILABLE = False


# ── Message bus ───────────────────────────────────────────────────────────

class MessageBus:
    """Per-project mailboxes with dual in-memory cache + JSONL persistence.

    Each agent (including "lead") has one file <workspace>/.mailboxes/<name>.jsonl.
    On startup the cache is rebuilt from disk so peek_inbox is O(1) on the
    hot path (idle poll reads inbox every 5s for every teammate). Writes
    append to BOTH the cache and the file under a portalocker file lock
    (cross-process safe); a thread lock guards the cache itself.

    If portalocker import or repeated lock acquisition fails, the bus
    degrades to lock-free mode: still functional within one process,
    but multi-process safety is no longer guaranteed. ``cross_process_safe``
    reflects the current mode for diagnostics.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.dir = self.workspace / ".mailboxes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory cache: agent_name → list of pending messages. The
        # cache is the source of truth for peek/drain; the file mirrors
        # it for cross-process visibility and crash recovery.
        self._cache: dict[str, list[dict]] = {}
        # File lock availability flag — flipped to False if portalocker
        # import or repeated acquires fail. Once degraded the bus keeps
        # running but loses cross-process safety.
        self.cross_process_safe: bool = _PORTALOCKER_AVAILABLE
        self._degrade_until: float = 0.0  # backoff timestamp
        # debug.8 Task A: optional hook fired after every successful send
        # to a "lead" recipient. Installed by TeammateSpawner so a
        # teammate's reply can flow into the lead's live SSE stream
        # without polling lead.jsonl. The hook receives the message dict.
        self._lead_hook: Callable[[dict], None] | None = None
        # Rebuild cache from existing .jsonl files so a process restart
        # doesn't lose unread messages.
        self._rebuild_cache_from_disk()

    def _rebuild_cache_from_disk(self) -> None:
        """Populate the in-memory cache from any pre-existing .jsonl
        files in the mailboxes dir. Called once at construction."""
        with self._lock:
            for path in self.dir.glob("*.jsonl"):
                if not path.is_file():
                    continue
                name = path.stem
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                msgs: list[dict] = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        # Skip a torn write from a prior crash. Better
                        # to lose one malformed line than to crash the
                        # whole bus on startup.
                        continue
                if msgs:
                    self._cache[name] = msgs

    def set_lead_hook(self, hook: Callable[[dict], None] | None) -> None:
        """Install/replace the lead-delivery hook (or pass None to clear)."""
        self._lead_hook = hook

    def _path(self, agent: str) -> Path:
        safe = "".join(c for c in agent if c.isalnum() or c in "._-") or "anon"
        return self.dir / f"{safe}.jsonl"

    @contextmanager
    def _try_file_lock(self, path: Path) -> Iterator[None]:
        """Cross-process file lock around a disk write/truncate.

        On repeated failure we degrade to lock-free mode for a backoff
        window so a flaky filesystem doesn't stall the bus forever.
        """
        # Already degraded within the backoff window → skip locking.
        if not self.cross_process_safe or time.time() < self._degrade_until:
            yield
            return
        if not _PORTALOCKER_AVAILABLE:
            yield
            return
        lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            with portalocker.Lock(str(lock_path), fail_when_locked=False,
                                  timeout=5.0):
                yield
        except Exception:
            # Lock failed — flip into degraded mode for 30s and proceed
            # unlocked. We still surface cross_process_safe=False so
            # callers can detect the loss of guarantee.
            self.cross_process_safe = False
            self._degrade_until = time.time() + 30.0
            yield

    def _append_disk(self, path: Path, msg: dict) -> None:
        """Append a single message to the agent's JSONL file under the
        cross-process lock. On lock failure we still attempt the write
        (degraded mode) so in-process semantics stay intact."""
        try:
            cm = self._try_file_lock(path)
        except Exception:
            # The lock helper itself raised (e.g. portalocker blew up).
            # Flip into degraded mode and write unlocked.
            self.cross_process_safe = False
            self._degrade_until = time.time() + 30.0
            cm = None
        if cm is None:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
            return
        with cm:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")

    def _truncate_disk(self, path: Path) -> None:
        """Atomically clear the JSONL file under the file lock."""
        try:
            cm = self._try_file_lock(path)
        except Exception:
            self.cross_process_safe = False
            self._degrade_until = time.time() + 30.0
            cm = None
        if cm is None:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            return
        with cm:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message",
             metadata: dict | None = None) -> dict:
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._lock:
            self._cache.setdefault(to_agent, []).append(msg)
            self._append_disk(self._path(to_agent), msg)
        # debug.8 Task A: fire the lead hook OUTSIDE the lock so a slow
        # sink can't stall mailbox writes for other agents. The hook is
        # only for messages addressed to "lead" — that's the only case
        # where the UI is waiting for live delivery.
        if to_agent == "lead" and self._lead_hook is not None:
            try:
                self._lead_hook(msg)
            except Exception:
                # A broken hook must never block mailbox delivery.
                pass
        return msg

    def read_inbox(self, agent: str) -> list[dict]:
        """Drain and return all messages for `agent`. Clears the cache
        and truncates the JSONL file."""
        path = self._path(agent)
        with self._lock:
            msgs = self._cache.pop(agent, [])
            if msgs:
                self._truncate_disk(path)
            elif path.exists():
                # Defensive: a write raced in via a non-cache path
                # (legacy message from before the upgrade). Clear it.
                self._truncate_disk(path)
        return msgs

    def peek_inbox(self, agent: str) -> list[dict]:
        """Read without draining. Returns a shallow copy so callers
        can't mutate the live cache."""
        with self._lock:
            return list(self._cache.get(agent, []))

    def broadcast(self, from_agent: str, to_agents: list[str],
                  content: str, msg_type: str = "message",
                  metadata: dict | None = None) -> list[str]:
        """Send the same message to multiple recipients atomically.

        debug.8 Task A: a teammate workflow needs a real broadcast (one
        send, many inboxes) so the lead can address the whole team at
        once without burning N send_message tool calls. Each copy:

        - is its own mailbox write (recipients see it independently)
        - carries ``metadata.broadcast = True`` and a shared
          ``broadcast_id`` so receivers can tell broadcast from unicast
          and decide whether to reply-to-all or to sender

        ``from_agent`` is excluded from recipients (silently) so a lead
        broadcasting to a list that happens to include itself doesn't
        end up reading its own broadcast. Duplicates are deduped to
        guard against careless callers. Returns the de-duplicated list
        of recipients actually written to (in input order, minus the
        sender).
        """
        # Dedupe while preserving order. dict.fromlist does both in one
        # pass on Python 3.7+ where dict preserves insertion order.
        seen: set[str] = set()
        unique: list[str] = []
        for who in to_agents:
            if who == from_agent or who in seen:
                continue
            seen.add(who)
            unique.append(who)
        if not unique:
            return []
        bid = uuid.uuid4().hex[:12]
        meta = dict(metadata or {})
        meta["broadcast"] = True
        meta["broadcast_id"] = bid
        ts = time.time()
        with self._lock:
            for who in unique:
                msg = {"from": from_agent, "to": who,
                       "content": content, "type": msg_type,
                       "ts": ts, "metadata": meta}
                self._cache.setdefault(who, []).append(msg)
                self._append_disk(self._path(who), msg)
        return unique


# ── Protocol state ────────────────────────────────────────────────────────

@dataclass
class ProtocolState:
    """One pending protocol request (plan approval or shutdown)."""
    request_id: str
    type: str           # "plan_approval" | "shutdown"
    sender: str         # who initiated the request
    target: str         # who must respond
    status: str         # "pending" | "approved" | "rejected"
    payload: str
    created_at: float = field(default_factory=time.time)


class ProtocolTracker:
    """Per-project registry of pending protocol requests.

    Both the lead-side tools (review_plan) and the teammate-side flow
    (submit_plan) read/write through this object so state stays
    consistent across threads.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, ProtocolState] = {}

    def new_request_id(self) -> str:
        return f"req_{uuid.uuid4().hex[:8]}"

    def register(self, state: ProtocolState) -> None:
        with self._lock:
            self._pending[state.request_id] = state

    def get(self, request_id: str) -> ProtocolState | None:
        with self._lock:
            return self._pending.get(request_id)

    def update_status(self, request_id: str, status: str) -> bool:
        with self._lock:
            s = self._pending.get(request_id)
            if s is None:
                return False
            s.status = status
            return True

    def list_pending(self) -> list[ProtocolState]:
        with self._lock:
            return [s for s in self._pending.values()
                    if s.status == "pending"]


# ── Spawner ───────────────────────────────────────────────────────────────

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
        # Hook fired on every bus.send when to_agent == "lead". Lets us
        # push the message into _lead_events without polling lead.jsonl.
        self.bus.set_lead_hook(self._emit_to_lead)

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
                    f"Use tools to complete the requested work. "
                    f"Send your final summary to 'lead' via send_message "
                    f"before stopping. After calling submit_plan, end "
                    f"your turn and wait for approval.</identity>")
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
        """
        deadline = time.time() + self.idle_timeout
        while time.time() < deadline:
            time.sleep(self.idle_poll_interval)
            inbox = self.bus.read_inbox(info.name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._send_shutdown_response(info.name, msg)
                        return ("shutdown", None)
                # Non-protocol (or other) messages: inject as next turn.
                return ("work", "<inbox>" + json.dumps(inbox) + "</inbox>")
            task = self._scan_unclaimed_tasks()
            if task is not None:
                claim_out = self._claim_task(task.id, info.name)
                if "Claimed" in claim_out:
                    wt_info = ""
                    if task.worktree:
                        wt_path = (Path(self.bus.workspace) / ".worktrees"
                                   / task.worktree)
                        wt_info = f"\nWork directory: {wt_path}"
                        if loop is not None and hasattr(loop, "set_worktree"):
                            loop.set_worktree(wt_path)
                    return ("work",
                            f"<auto-claimed>Task {task.id}: "
                            f"{task.subject}{wt_info}</auto-claimed>")
        return ("timeout", None)

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


__all__ = ["MessageBus", "ProtocolState", "ProtocolTracker",
           "TeammateInfo", "TeammateSpawner", "convention_prompt"]


# ── Built-in teammate convention ─────────────────────────────────────────

_CONVENTION_PROMPT = """<convention>
You are a teammate in a multi-agent team. The following rules apply on
EVERY turn, regardless of the task you were spawned for:

1. Plan first, then act. Before doing any non-trivial work (writing
   files, running tools that mutate state, performing multi-step
   research), call `submit_plan` with a concise plan and end your
   turn. Wait for the lead's `review_plan` verdict before proceeding.
   Trivial work (a single read-only tool call, a direct answer) does
   not require a plan.

2. Report results to the lead. When your task is complete (or you hit
   a blocker you cannot resolve), call
   `send_message(to="lead", content=<summary>, msg_type="result")`
   with a brief summary of what you did and any artifacts produced.

3. Honor shutdown. If your inbox contains a `shutdown_request` message
   or the lead sends a `shutdown` directive, acknowledge it and stop.
   Do not start new work after receiving shutdown.

4. Read your inbox each turn. The lead may inject messages (mention
   routing, plan verdicts, follow-up tasks) via your mailbox between
   turns. Treat anything in your inbox as authoritative input from the
   lead; if a message is labeled `mention`, it carries a new task you
   should pick up.

5. Stay in your lane. Do not spawn further teammates, do not call
   review_plan (that's the lead's tool), do not modify other
   teammates' worktrees. Your scope is the task you were assigned.

These rules are non-negotiable; the user-supplied prompt that follows
describes WHAT to do, not how to interact with the team.
</convention>"""


def convention_prompt() -> str:
    """Return the shared teammate convention prompt.

    Single source of truth so prompt authors focus on task content.
    The spawner's _runner prepends this to the user-provided prompt;
    tests assert it contains the protocol keywords (submit_plan,
    send_message, shutdown, inbox) so the convention can't silently
    drift away from what the spawner expects.
    """
    return _CONVENTION_PROMPT
