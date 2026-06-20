"""SessionManager — owns live AgentLoop instances keyed by (project_id,
session_id). Per-project locking serializes concurrent sends to the same
project; different projects run in parallel.

Sessions are *resumable* across server restarts: messages and todos live
on disk under project storage, and a sessions index records created /
last-active / message-count per session. Cold sessions are warmed on
demand via `_ensure_warm` (used by send / explicit resume / idempotent
start).
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

from ..core.loop import AgentLoop, repair_dangling_tool_uses
from ..projects import ProjectManager
from ..storage import SessionMeta
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
        """Start a new session, or re-warm an existing one idempotently.

        If `session_id` is provided and that session already exists on
        disk, this is a resume — the existing AgentLoop is reconstructed
        from persisted state. If new, an empty transcript is seeded.
        """
        # Validate project exists
        project = self.pm.get(project_id)
        session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"

        # Idempotent resume: if session exists on disk, warm it.
        existing = {m.session_id for m in project.storage.list_sessions(project_id)}
        if session_id in existing:
            return self._warm(project, session_id,
                              tools=tools, on_event=on_event, model=model)

        # New session — seed an empty transcript so list_sessions sees it.
        loop = AgentLoop(project.as_ref(), session_id,
                         tools=tools, on_event=on_event, model=model,
                         hooks=project.hooks)
        # Persist the (currently empty) transcript to seed the index.
        project.storage.save_messages(project_id, session_id, loop.messages)
        sess = Session(project_id=project_id, session_id=session_id, loop=loop)
        self._sessions[(project_id, session_id)] = sess
        return sess

    def get(self, project_id: str, session_id: str) -> Session:
        """In-memory only. Use `_ensure_warm` for cold-resume semantics."""
        key = (project_id, session_id)
        if key not in self._sessions:
            raise KeyError(f"session not found: {key}")
        return self._sessions[key]

    def _warm(self, project, session_id: str,
              tools: list[Tool] | None = None,
              on_event: Callable[[dict], None] | None = None,
              model: str | None = None) -> Session:
        """Construct an AgentLoop for an existing on-disk session, with
        mid-turn-crash auto-repair applied once at load time."""
        loop = AgentLoop(project.as_ref(), session_id,
                         tools=tools, on_event=on_event, model=model,
                         hooks=project.hooks)
        if repair_dangling_tool_uses(loop.messages):
            # Repair changed the transcript; persist so subsequent
            # restarts don't re-repair.
            project.storage.save_messages(project.project_id, session_id,
                                          loop.messages)
        sess = Session(project_id=project.project_id,
                       session_id=session_id, loop=loop)
        self._sessions[(project.project_id, session_id)] = sess
        return sess

    def _ensure_warm(self, project_id: str, session_id: str) -> Session:
        """Get a warm session, lazy-loading from disk if needed. Raises
        KeyError if the session is neither in memory nor on disk."""
        key = (project_id, session_id)
        if key in self._sessions:
            return self._sessions[key]
        project = self.pm.get(project_id)
        existing = {m.session_id for m in project.storage.list_sessions(project_id)}
        if session_id not in existing:
            raise KeyError(f"session not found: {key}")
        return self._warm(project, session_id)

    def list(self, project_id: str) -> list[SessionMeta]:
        """Disk truth + in-memory flag. Sessions that exist on disk but
        aren't currently warmed appear with in_memory=False."""
        project = self.pm.get(project_id)
        metas = project.storage.list_sessions(project_id)
        warm_ids = {sid for (pid, sid) in self._sessions if pid == project_id}
        for m in metas:
            m.in_memory = m.session_id in warm_ids
        # Stable order: most recently active first.
        metas.sort(key=lambda m: m.last_active_at, reverse=True)
        return metas

    def stop(self, project_id: str, session_id: str) -> None:
        sess = self._sessions.get((project_id, session_id))
        if sess:
            sess.stop()

    def remove(self, project_id: str, session_id: str) -> bool:
        """Stop the session (if running), unregister it from memory, and
        delete its on-disk metadata + transcript. Returns True if
        anything was removed."""
        project = self.pm.get(project_id)
        sess = self._sessions.pop((project_id, session_id), None)
        if sess is not None:
            sess.stop()
        existed_on_disk = session_id in {m.session_id for m
                                         in project.storage.list_sessions(project_id)}
        if existed_on_disk:
            # Removes messages + todos + sessions-index entry atomically.
            project.storage.delete_session(project_id, session_id)
        return sess is not None or existed_on_disk

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
        different projects run in parallel. Auto-warms cold sessions."""
        sess = self._ensure_warm(project_id, session_id)
        with self._lock_for(project_id):
            for ev in sess.loop.run(user_input):
                yield ev
