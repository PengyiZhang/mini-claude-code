"""Per-project JSONL mailboxes + file locking."""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:
    import portalocker  # type: ignore
    _PORTALOCKER_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only when dep missing
    portalocker = None  # type: ignore
    _PORTALOCKER_AVAILABLE = False


def _format_inbox_as_dialogue(inbox: list[dict], recipient: str) -> str:
    """Render an inbox batch as readable dialogue for the recipient's
    next turn's ``user_input``.

    The default ``user_input`` injected by ``_idle_poll`` used to be a
    raw JSON dump wrapped in ``<inbox>…</inbox>`` tags. That JSON
    surfaced verbatim in the chat panel when viewing the teammate's
    session, and the LLM had to parse the JSON to know who said what.
    This helper formats each message as a labelled line so:

    * The chat bubble reads as natural conversation (``lead said: …``).
    * The LLM sees the same content with explicit sender / type cues.
    * The original JSON is appended at the end as a structured
      fallback for any tooling that still expects it.

    The ``<inbox>…</inbox>`` envelope is preserved so prompt-level
    parsing rules (e.g. "check your <inbox> tag") keep working.
    """
    lines = [f"<inbox>"]
    lines.append(f"{len(inbox)} message(s) for @{recipient}:")
    for i, msg in enumerate(inbox, 1):
        sender = msg.get("from", "?")
        msg_type = msg.get("type", "message")
        content = msg.get("content", "")
        meta = msg.get("metadata") or {}
        cc_note = ""
        if meta.get("cc"):
            orig = meta.get("original_to", "?")
            cc_note = f" (CC of a message originally to {orig})"
        lines.append(
            f"[{i}] {sender} said (type: {msg_type}){cc_note}: {content}"
        )
    lines.append("</inbox>")
    # Structured fallback. Indented under the readable header so the
    # LLM sees the human-readable form first when scanning top-down.
    # ensure_ascii=False: this line is persisted verbatim in the
    # teammate's transcript (it becomes part of user_input). With the
    # json.dumps default (ensure_ascii=True) any non-ASCII content —
    # Chinese text from the lead or peers — was written as literal
    # `你好` escape sequences and rendered as garbled "JSON
    # ASCII" text in the chat bubble after a page refresh.
    lines.append("<!-- raw: " + json.dumps(inbox, ensure_ascii=False) + " -->")
    return "\n".join(lines)


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

    # Maximum number of history entries kept per agent. History logs
    # outlive the live inbox and would otherwise grow unbounded across
    # the lifetime of a project. Older entries are dropped FIFO.
    _HISTORY_CAP = 200
    # Phase I.A: msg_types that trigger an automatic CC to "lead" when
    # sent between teammates. Plain chitchat (msg_type=message) and
    # coordination types (shutdown_*, plan_approval_*, mention) stay
    # private — lead only needs to know about progress signals.
    _LEAD_CC_TYPES: frozenset[str] = frozenset({"result", "milestone", "blocker"})

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.dir = self.workspace / ".mailboxes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory cache: agent_name → list of pending messages. The
        # cache is the source of truth for peek/drain; the file mirrors
        # it for cross-process visibility and crash recovery.
        self._cache: dict[str, list[dict]] = {}
        # Append-only history (debug.8 Task B): per-agent log of every
        # message ever received, surviving read_inbox drains and process
        # restarts. Used by /agents inbox <name> and TeammatesPanel so
        # the lead can review messages that arrived while they were
        # looking elsewhere — the live inbox is drained by the
        # teammate's own _idle_poll within seconds of arrival.
        self._history: dict[str, list[dict]] = {}
        # Per-agent "sent a result since their last take" flag. Set in
        # send() when msg_type == "result"; drained per-agent via
        # take_result_flag(agent) by the teammate runner so it can park
        # after reporting task completion. Per-agent (not a global drain)
        # so concurrent teammates don't clear each other's signal.
        self._result_pending: set[str] = set()
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
        self._rebuild_history_from_disk()

    def _rebuild_cache_from_disk(self) -> None:
        """Populate the in-memory cache from any pre-existing .jsonl
        files in the mailboxes dir. Called once at construction."""
        with self._lock:
            for path in self.dir.glob("*.jsonl"):
                if not path.is_file():
                    continue
                # Skip history files — they're handled by
                # _rebuild_history_from_disk to avoid double-parsing.
                if path.name.endswith(".history.jsonl"):
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

    def _rebuild_history_from_disk(self) -> None:
        """Populate the history cache from <name>.history.jsonl files.

        Lives parallel to the live inbox (.jsonl) but is never drained.
        Capped at _HISTORY_CAP entries per agent on rebuild — older
        files past the cap keep their full contents on disk (we don't
        rewrite them on load), but the cache only holds the tail.
        """
        with self._lock:
            for path in self.dir.glob("*.history.jsonl"):
                if not path.is_file():
                    continue
                # Strip the ".history" infix to recover the agent name.
                # path.stem is "<name>.history" → drop the suffix.
                stem = path.stem
                name = stem[:-len(".history")] if stem.endswith(".history") else stem
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
                        continue
                if msgs:
                    # Apply the cap on rebuild so a long-running project
                    # doesn't pin unbounded history into memory.
                    if len(msgs) > self._HISTORY_CAP:
                        msgs = msgs[-self._HISTORY_CAP:]
                    self._history[name] = msgs

    def set_lead_hook(self, hook: Callable[[dict], None] | None) -> None:
        """Install/replace the lead-delivery hook (or pass None to clear)."""
        self._lead_hook = hook

    def _path(self, agent: str) -> Path:
        safe = "".join(c for c in agent if c.isalnum() or c in "._-") or "anon"
        return self.dir / f"{safe}.jsonl"

    def _history_path(self, agent: str) -> Path:
        """Path to the append-only history log for ``agent``. Kept
        separate from _path() so draining the live inbox never wipes
        the history."""
        safe = "".join(c for c in agent if c.isalnum() or c in "._-") or "anon"
        return self.dir / f"{safe}.history.jsonl"

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
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            return
        with cm:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

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
        # Phase I.A: determine whether this send triggers an auto-CC to
        # lead. CC fires only for teammate→teammate progress signals
        # (result/milestone/blocker); if lead is already the direct
        # recipient there's nothing to copy. The CC copy is what the
        # lead hook (if any) should see, since downstream consumers
        # expect a lead-addressed payload.
        cc_msg: dict | None = None
        if (to_agent != "lead"
                and from_agent != "lead"
                and msg_type in self._LEAD_CC_TYPES):
            cc_meta = dict(msg["metadata"])
            cc_meta["cc"] = True
            cc_meta["original_to"] = to_agent
            cc_msg = {"from": from_agent, "to": "lead",
                      "content": content, "type": msg_type,
                      "ts": msg["ts"], "metadata": cc_meta}
        with self._lock:
            self._cache.setdefault(to_agent, []).append(msg)
            self._append_disk(self._path(to_agent), msg)
            # Record a result-send for the SENDER so its runner can park
            # it after this turn (park-after-result). Checked per-agent
            # via take_result_flag; cleared on take.
            if msg_type == "result":
                self._result_pending.add(from_agent)
            # Append to the per-agent history log (debug.8 Task B). The
            # history survives read_inbox drains and process restarts
            # so /agents inbox <name> can show every received message
            # even after the teammate's _idle_poll has drained the live
            # inbox.
            hist = self._history.setdefault(to_agent, [])
            hist.append(msg)
            if len(hist) > self._HISTORY_CAP:
                # Drop oldest in memory; the on-disk file keeps the full
                # tail (truncation is expensive and not worth the disk
                # traffic — see _rebuild_history_from_disk which re-cap
                # on next restart).
                del hist[0:len(hist) - self._HISTORY_CAP]
            self._append_disk(self._history_path(to_agent), msg)
        # Phase I.A: write the CC copy to lead's mailbox + history.
        # Separate lock block so the (potential) lead hook below still
        # fires OUTSIDE the lock — same invariant as the original.
        if cc_msg is not None:
            with self._lock:
                self._cache.setdefault("lead", []).append(cc_msg)
                self._append_disk(self._path("lead"), cc_msg)
                lead_hist = self._history.setdefault("lead", [])
                lead_hist.append(cc_msg)
                if len(lead_hist) > self._HISTORY_CAP:
                    del lead_hist[0:len(lead_hist) - self._HISTORY_CAP]
                self._append_disk(self._history_path("lead"), cc_msg)
        # debug.8 Task A: fire the lead hook OUTSIDE the lock so a slow
        # sink can't stall mailbox writes for other agents. The hook is
        # only for messages addressed to "lead" — that's the only case
        # where the UI is waiting for live delivery. Phase I.A: when a
        # CC copy was made, the hook sees that copy (lead-addressed)
        # rather than the original teammate-addressed message; this
        # keeps the hook payload shape stable for downstream consumers.
        hook_msg = cc_msg if cc_msg is not None else msg
        if hook_msg["to"] == "lead" and self._lead_hook is not None:
            try:
                self._lead_hook(hook_msg)
            except Exception:
                # A broken hook must never block mailbox delivery.
                pass
        return msg

    def history(self, agent: str) -> list[dict]:
        """Return a copy of every message ever sent to ``agent`` (in
        send order). Survives read_inbox drains and process restarts.

        Capped at _HISTORY_CAP entries; oldest dropped FIFO. The
        returned list is a shallow copy — callers may mutate freely.
        """
        with self._lock:
            return list(self._history.get(agent, []))

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

    def take_result_flag(self, agent: str) -> bool:
        """Atomically read+clear the per-agent "sent a result" flag.

        Returns True if ``agent`` sent a ``msg_type="result"`` message
        since the last take, False otherwise. The teammate runner uses
        this after each turn to decide whether to park the teammate
        (park-after-result: a finished teammate stops running LLM turns
        until the lead @mentions new work). Per-agent so concurrent
        teammates can't race-clear each other's flag.
        """
        with self._lock:
            was = agent in self._result_pending
            self._result_pending.discard(agent)
            return was

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
