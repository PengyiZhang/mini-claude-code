"""Teams subsystem — per-project message bus and teammate spawner.

Ports s20 lines 491-872 with simplifications:
- MessageBus is per-project (instance on Project), not module-global
- Mailboxes live under <workspace>/.mailboxes/<agent>.jsonl
- Teammate spawner runs a real sub-AgentLoop in a daemon thread
- Plan-approval protocol state machine is deferred (TODO: follow-up)
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


@dataclass
class TeammateInfo:
    name: str
    role: str
    alive: bool = True
    thread: threading.Thread | None = None
    started_at: float = field(default_factory=time.time)


class TeammateSpawner:
    """Per-project registry of running teammates.

    Each teammate runs a sub-AgentLoop in a daemon thread. The spawner
    monitors a control inbox for shutdown_request messages; when one arrives
    it stops the loop after the current turn.

    Simplified vs s20: no plan-approval gate, no autonomous idle task polling.
    Those land in a follow-up.

    The `loop_factory` callable takes a session_id string and returns a fresh
    AgentLoop. Project supplies this so the spawner doesn't need a hard
    reference to the ProjectRef (which doesn't exist yet at construction
    time).
    """

    def __init__(self, workspace: Path,
                 loop_factory: Callable[[str], "AgentLoop"]):
        self.bus = MessageBus(workspace)
        self._loop_factory = loop_factory
        self._lock = threading.Lock()
        self._teammates: dict[str, TeammateInfo] = {}

    def list_alive(self) -> list[TeammateInfo]:
        with self._lock:
            return [t for t in self._teammates.values() if t.alive]

    def spawn(self, name: str, role: str, prompt: str,
              on_event=None) -> str | None:
        """Spawn a teammate. Returns None on success, error string on dup."""
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
        self.bus.send("lead", name, "Shut down.", "shutdown_request",
                      {"request_id": f"req_{uuid.uuid4().hex[:8]}"})
        return f"Shutdown request sent to {name}"

    def _runner(self, info: TeammateInfo, prompt: str, on_event):
        loop = self._loop_factory(f"teammate:{info.name}")
        identity = (f"<identity>You are '{info.name}', a {info.role}. "
                    f"Use tools to complete the requested work. "
                    f"Send your final summary to 'lead' via send_message "
                    f"before stopping.</identity>")
        try:
            user_turn = f"{identity}\n\n{prompt}"
            # Drain any pre-spawn messages first so shutdown works.
            for msg in self.bus.read_inbox(info.name):
                if msg.get("type") == "shutdown_request":
                    self.bus.send(info.name, "lead", "Shutting down.",
                                  "shutdown_response", {"approve": True})
                    return
            for ev in loop.run(user_turn):
                if on_event is not None:
                    on_event(ev)
                # Check for shutdown between turns.
                if ev.get("type") == "done":
                    pending = self.bus.peek_inbox(info.name)
                    if any(m.get("type") == "shutdown_request"
                           for m in pending):
                        self.bus.read_inbox(info.name)
                        self.bus.send(info.name, "lead", "Shutting down.",
                                      "shutdown_response",
                                      {"approve": True})
                        return
        except Exception as e:
            self.bus.send(info.name, "lead",
                          f"[error] {type(e).__name__}: {e}", "result")
        finally:
            with self._lock:
                if info.name in self._teammates:
                    info = self._teammates[info.name]
                    info.alive = False
            self.bus.send(info.name, "lead", "Done.", "result")


__all__ = ["MessageBus", "TeammateInfo", "TeammateSpawner"]

