"""Pending plan-approval / shutdown protocol requests, keyed by id."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


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
