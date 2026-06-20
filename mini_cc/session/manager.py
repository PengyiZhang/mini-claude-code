"""SessionManager — owns live AgentLoop instances keyed by (project_id,
session_id). Per-project locking serializes concurrent sends to the same
project; different projects run in parallel.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

from ..core.loop import AgentLoop
from ..projects import ProjectManager
from ..tools import Tool


@dataclass
class Session:
    project_id: str
    session_id: str
    loop: AgentLoop

    def stop(self):
        self.loop.stop()


class SessionManager:
    def __init__(self, project_manager: ProjectManager):
        self.pm = project_manager
        self._sessions: dict[tuple[str, str], Session] = {}
        self._project_locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, project_id: str) -> threading.RLock:
        with self._guard:
            if project_id not in self._project_locks:
                self._project_locks[project_id] = threading.RLock()
            return self._project_locks[project_id]

    def start_session(self, project_id: str,
                      session_id: str | None = None,
                      tools: list[Tool] | None = None,
                      on_event: Callable[[dict], None] | None = None,
                      model: str | None = None) -> Session:
        # Validate project exists
        self.pm.get(project_id)
        session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        project = self.pm.get(project_id)
        loop = AgentLoop(project.as_ref(), session_id,
                         tools=tools, on_event=on_event, model=model,
                         hooks=project.hooks)
        sess = Session(project_id=project_id, session_id=session_id, loop=loop)
        self._sessions[(project_id, session_id)] = sess
        return sess

    def get(self, project_id: str, session_id: str) -> Session:
        key = (project_id, session_id)
        if key not in self._sessions:
            raise KeyError(f"session not found: {key}")
        return self._sessions[key]

    def list(self, project_id: str) -> list[str]:
        return [sid for (pid, sid) in self._sessions if pid == project_id]

    def stop(self, project_id: str, session_id: str) -> None:
        sess = self._sessions.get((project_id, session_id))
        if sess:
            sess.stop()

    def remove(self, project_id: str, session_id: str) -> bool:
        """Stop the session (if running) and unregister it. Returns True
        if a session was present, False otherwise."""
        sess = self._sessions.pop((project_id, session_id), None)
        if sess is None:
            return False
        sess.stop()
        return True

    def try_lock(self, project_id: str) -> bool:
        """Non-blocking probe of the per-project send lock. Returns True
        if the lock is currently free, False if another send holds it.

        The probe acquires-then-releases so it does not itself block
        subsequent sends. Used by HTTP transports to return 409 instead
        of blocking an HTTP worker thread on a busy project.
        """
        lock = self._lock_for(project_id)
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        return got

    def send(self, project_id: str, session_id: str,
             user_input: str) -> Iterator[dict]:
        """Send a user turn and stream events. Same project_id serializes;
        different projects run in parallel."""
        sess = self.get(project_id, session_id)
        with self._lock_for(project_id):
            for ev in sess.loop.run(user_input):
                yield ev
