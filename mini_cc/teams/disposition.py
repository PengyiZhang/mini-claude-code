"""Per-message disposition tracking for teammate inboxes.

The TeammatesPanel shows whether each inbox message has been
acknowledged or ignored by the lead. This module persists that state
to a sidecar JSON file so it survives process restarts even after the
inbox itself is drained.

State values: "unread" (default) | "read" | "ignored".

Keys: message timestamps (float). MessageBus.send stamps time.time()
on every message and we never reuse a stamp within a process, so ts is
a stable per-message identifier.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


_VALID_STATES = frozenset({"read", "ignored"})


class InboxDisposition:
    """Sidecar JSON recording per-(agent, ts) disposition state.

    File layout: <mailboxes_dir>/dispositions.json holding
    ``{agent: {ts_str: state}}``. We keep this separate from the
    agent's .jsonl so draining the inbox doesn't wipe the disposition
    history — the user may want to revisit "what did I already act on"
    after alice's mailbox is drained.
    """

    def __init__(self, mailboxes_dir: Path):
        self.dir = Path(mailboxes_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "dispositions.json"
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError):
            # Torn write from a previous crash — start empty. Better
            # to re-show messages as unread than to crash the bus.
            return
        if isinstance(parsed, dict):
            for agent, m in parsed.items():
                if not isinstance(m, dict):
                    continue
                self._state[agent] = {
                    str(k): v for k, v in m.items()
                    if isinstance(v, str) and v in _VALID_STATES
                }

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.path)

    def state_for(self, agent: str, ts: float) -> str:
        """Return 'unread' | 'read' | 'ignored' for a (agent, ts) pair."""
        with self._lock:
            m = self._state.get(agent, {})
            return m.get(str(ts), "unread")

    def mark_read(self, agent: str, ts: float) -> None:
        with self._lock:
            self._state.setdefault(agent, {})[str(ts)] = "read"
            self._save_locked()

    def mark_ignored(self, agent: str, ts: float) -> None:
        with self._lock:
            self._state.setdefault(agent, {})[str(ts)] = "ignored"
            self._save_locked()

    def dispositions_for(self, agent: str) -> dict[str, str]:
        """Return all known dispositions for ``agent``. Keys are ts-as-str."""
        with self._lock:
            return dict(self._state.get(agent, {}))

    def clear(self, agent: str) -> None:
        """Drop all dispositions for an agent (used when deleting the
        teammate). Keeps the sidecar from growing unbounded."""
        with self._lock:
            if agent in self._state:
                del self._state[agent]
                self._save_locked()
