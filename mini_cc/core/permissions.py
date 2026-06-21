"""Interactive permission prompts — per-session registry of pending
tool-use permission requests.

Used by AgentLoop when a tool in the project's `prompt_tools` set is
invoked: the loop yields a `permission_request` event, then blocks on
`PermissionInterceptor.wait()` until either:

- `decide()` is called from an external decider (HTTP route, IPC, etc.)
  with "allow" or "deny", or
- `timeout_seconds` elapses (returns None — caller treats as deny
  with "[permission timed out]"), or
- `cancel()` is called or `stop_event` is set (session stop / client
  disconnect — returns None).

The interceptor is per-project (lives on Project, threaded through
ProjectRef). One interceptor can serve multiple sessions; requests
are keyed by `request_id` (UUID4 hex) and scoped by `session_id` for
`list_pending`.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..storage import Storage  # noqa: F401  (type-only, for docs)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class PermissionRequest:
    request_id: str
    session_id: str
    tool_name: str
    tool_input: dict
    created_at: str
    _decided: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None       # "allow" | "deny"
    deny_message: str | None = None

    def summary(self) -> dict:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "created_at": self.created_at,
        }


class PermissionInterceptor:
    """Per-project registry of pending permission requests."""

    def __init__(self, timeout_seconds: int = 300):
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, PermissionRequest] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str, tool_name: str,
               tool_input: dict) -> PermissionRequest:
        req = PermissionRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=dict(tool_input or {}),
            created_at=_iso_now(),
        )
        with self._lock:
            self._pending[req.request_id] = req
        return req

    def wait(self, request_id: str,
             stop_event: threading.Event | None = None,
             poll_interval: float = 1.0) -> PermissionRequest | None:
        """Block until the request is decided, cancelled, or the
        timeout elapses. Returns the decided request (with .decision
        and .deny_message filled in), or None on timeout / cancel /
        unknown id.

        Polls every `poll_interval` seconds so `stop_event` can
        interrupt the wait within ~poll_interval. Caller is expected
        to treat None as "deny with [timed out] message".

        Pops the request from the registry on exit so subsequent
        list_pending calls don't show it.
        """
        with self._lock:
            req = self._pending.get(request_id)
        if req is None:
            return None
        import time
        deadline = None
        if self.timeout_seconds > 0:
            deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                # Already decided?
                if req._decided.is_set():
                    return req
                cap = poll_interval
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None  # timed out
                    cap = min(cap, remaining)
                if stop_event is not None and stop_event.is_set():
                    return None
                req._decided.wait(timeout=cap)
        finally:
            # On any exit path, drop from registry. If decide() already
            # set the event, we return the req with its decision; if
            # timeout/cancel, we return None but still clean up.
            with self._lock:
                self._pending.pop(request_id, None)

    def decide(self, request_id: str, decision: str,
               message: str = "") -> bool:
        """Record a decision. Returns True on success, False if the
        request is unknown or already decided. The request stays in
        the registry until the waiter wakes and pops it."""
        if decision not in ("allow", "deny"):
            raise ValueError("decision must be 'allow' or 'deny'")
        with self._lock:
            req = self._pending.get(request_id)
            if req is None or req.decision is not None:
                return False
            req.decision = decision
            req.deny_message = message or None
            req._decided.set()
        return True

    def cancel(self, request_id: str) -> None:
        """Wake any waiter on this request with decision='deny' and
        message='[cancelled by session stop]'. Used when the session
        stops or the SSE worker bails. The waiter will pop the
        request from the registry on its way out."""
        with self._lock:
            req = self._pending.get(request_id)
        if req is not None:
            req.decision = "deny"
            req.deny_message = "[cancelled by session stop]"
            req._decided.set()

    def list_pending(self, session_id: str | None = None) -> list[PermissionRequest]:
        with self._lock:
            # Only show undecided requests; decided ones are waiting for
            # the waiter to pop them and shouldn't be re-listed.
            items = [r for r in self._pending.values() if r.decision is None]
        if session_id is not None:
            items = [r for r in items if r.session_id == session_id]
        return items
