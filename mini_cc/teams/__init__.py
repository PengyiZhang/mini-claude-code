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
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..core.loop import AgentLoop
    from ..storage import Storage, Task


# ── Message bus ───────────────────────────────────────────────────────────

class MessageBus:
    """Per-project append-only JSONL mailboxes.

    Each agent (including "lead") has one file <workspace>/.mailboxes/<name>.jsonl.
    read_inbox drains and deletes the file so each message is consumed once.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.dir = self.workspace / ".mailboxes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, agent: str) -> Path:
        safe = "".join(c for c in agent if c.isalnum() or c in "._-") or "anon"
        return self.dir / f"{safe}.jsonl"

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message",
             metadata: dict | None = None) -> dict:
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        with self._lock:
            with self._path(to_agent).open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
        return msg

    def read_inbox(self, agent: str) -> list[dict]:
        """Drain and return all messages for `agent`. Deletes the mailbox file."""
        path = self._path(agent)
        if not path.exists():
            return []
        with self._lock:
            text = path.read_text(encoding="utf-8")
            path.unlink()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def peek_inbox(self, agent: str) -> list[dict]:
        """Read without draining (used by tests and idle polling)."""
        path = self._path(agent)
        if not path.exists():
            return []
        with self._lock:
            text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


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

    # ── Public API (used by lead tools) ────────────────────────────────
    def list_alive(self) -> list[TeammateInfo]:
        with self._lock:
            return [t for t in self._teammates.values() if t.alive]

    def spawn(self, name: str, role: str, prompt: str,
              on_event=None) -> str | None:
        with self._lock:
            existing = self._teammates.get(name)
            if existing is not None and existing.alive:
                return f"Teammate '{name}' already exists"
        info = TeammateInfo(name=name, role=role)
        thread = threading.Thread(
            target=self._runner, args=(info, prompt, on_event),
            daemon=True, name=f"teammate:{name}")
        info.thread = thread
        with self._lock:
            self._teammates[name] = info
        thread.start()
        return None

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

    def list_pending_requests(self) -> list[ProtocolState]:
        return self.protocol.list_pending()

    # ── Runner (worker thread body) ────────────────────────────────────
    def _runner(self, info: TeammateInfo, prompt: str, on_event):
        # Hyphen not colon: see core/subagent.py for the validate_id rationale.
        loop = self._loop_factory(f"teammate-{info.name}")
        identity = (f"<identity>You are '{info.name}', a {info.role}. "
                    f"Use tools to complete the requested work. "
                    f"Send your final summary to 'lead' via send_message "
                    f"before stopping. After calling submit_plan, end "
                    f"your turn and wait for approval.</identity>")
        try:
            # Pre-spawn drain: handle any pending protocol messages.
            for msg in self.bus.read_inbox(info.name):
                if msg.get("type") == "shutdown_request":
                    self._send_shutdown_response(info.name, msg)
                    return

            next_input = f"{identity}\n\n{prompt}"
            should_shutdown = False

            while not should_shutdown:
                # Run one full turn (model + tool calls). Mid-turn
                # shutdown detection lets a long-running generator exit
                # as soon as the lead requests shutdown.
                for ev in loop.run(next_input):
                    if on_event is not None:
                        on_event(ev)
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
           "TeammateInfo", "TeammateSpawner"]
