"""Second-precision one-shot scheduling for AgentLoop self-pacing.

Distinct from CronScheduler (minute-precision, cron expressions): this
module handles "remind me in N seconds" patterns that an agent uses to
poll a long-running build, pace a /loop iteration, or wake itself up
after an external event.

All state is in-memory — wakeups don't survive process restarts by
design. Durable scheduling should use CronScheduler.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Wakeup:
    wakeup_id: str
    prompt: str
    fire_at: float                    # monotonic deadline
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    fired: bool = False


class WakeupScheduler:
    """In-memory per-project (or per-loop) one-shot wakeups.

    Not thread-safe across multiple loops — each AgentLoop should own
    its own instance (passed via ProjectRef).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wakeups: dict[str, Wakeup] = {}

    def schedule(self, prompt: str, delay_seconds: float,
                 *, reason: str = "", base_time: float | None = None) -> str:
        """Schedule a one-shot wakeup. Returns the wakeup_id.

        ``base_time`` overrides the monotonic clock base — useful for
        tests that want to control fire timing precisely. Production
        callers leave it None.
        """
        if delay_seconds < 1:
            delay_seconds = 1
        start = base_time if base_time is not None else time.monotonic()
        fire_at = start + delay_seconds
        wakeup_id = f"wakeup_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._wakeups[wakeup_id] = Wakeup(
                wakeup_id=wakeup_id, prompt=prompt,
                fire_at=fire_at, reason=reason)
        return wakeup_id

    def cancel(self, wakeup_id: str) -> bool:
        with self._lock:
            return self._wakeups.pop(wakeup_id, None) is not None

    def list(self) -> list[Wakeup]:
        with self._lock:
            return list(self._wakeups.values())

    def tick(self, now: float | None = None) -> list[Wakeup]:
        """Return and remove all wakeups whose deadline has passed.

        ``now`` defaults to time.monotonic(); callers can pass a fixed
        clock for tests.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            due = [w for w in self._wakeups.values() if w.fire_at <= now]
            for w in due:
                w.fired = True
                self._wakeups.pop(w.wakeup_id, None)
        return due

    def next_deadline(self) -> float | None:
        """Return the earliest upcoming fire_at, or None if empty.

        Useful for the loop to know how long to sleep before ticking again.
        """
        with self._lock:
            if not self._wakeups:
                return None
            return min(w.fire_at for w in self._wakeups.values())
