"""LeadWatcher daemon — Phase I.B-1.2.

Polls lead's mailbox and nudges the lead's AgentLoop when teammate
result/milestone/blocker messages land while the user is away from
``/send``. Closes the loop between Phase I.A's auto-CC mechanism (which
copies teammate→teammate progress signals into lead's mailbox with
``metadata.cc = True``) and Phase I.B-1.1's ``AgentLoop.nudge`` entry
point.

Threading model
---------------
A single daemon thread per project (``LeadWatcher-<project_id>``) loops:

1. ``_stop.wait(poll_interval)`` — interruptible sleep so ``stop()``
   returns promptly.
2. Peek lead's mailbox (``bus.peek_inbox("lead")`` — non-destructive).
3. If the peek contains at least one triggering message (``metadata.cc
   == True`` OR ``msg_type in {result, milestone, blocker}``), wait
   ``debounce`` seconds (interruptible) then DRAIN and nudge.
4. Otherwise, loop — no drain. Non-triggering batches (e.g. plain chat
   replies from a teammate) stay in the mailbox for the lead's own
   ``_inject_teammate_replies`` path on the next user ``/send``.

Drain semantics (Option C in the design notes)
----------------------------------------------
When the watcher fires it drains the ENTIRE mailbox (direct replies
AND CC'd copies) and synthesises a single nudge listing every drained
message. This is consistent with how ``MessageBus`` is meant to be
consumed (``read_inbox`` is all-or-nothing) and means the lead's own
``_inject_teammate_replies`` path will see an empty mailbox on the next
user ``/send`` — the nudge already delivered the content.

TOCTOU handling
---------------
Between the detect-on-peek and the post-debounce drain, another thread
(typically the lead's ``/send`` handler) may have drained the mailbox.
We re-check emptiness right before nudging: if the mailbox was emptied
mid-debounce (the user came back), the watcher does NOT nudge. The
user is present, their own ``/send`` will surface the content via the
normal injection path; nudging would either double-deliver or race
with the in-flight turn.

We also resolve ``lead_loop_getter()`` BEFORE draining: if no lead
session is bound (returns ``None``), we skip without touching the
mailbox so the next ``/send`` finds the messages intact.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..core.loop import AgentLoop
    from . import MessageBus


# Message types the watcher treats as triggers. Anything CC'd by the
# Phase I.A mechanism is also a trigger regardless of type (matches the
# spec: "any msg with metadata.cc == True OR msg_type in {result,
# milestone, blocker}").
_TRIGGER_TYPES: frozenset[str] = frozenset({"result", "milestone", "blocker"})


def _is_trigger(msg: dict) -> bool:
    """True if ``msg`` should wake the watcher's debounce + nudge path.

    Returns False for messages the watcher already re-injected after a
    failed nudge — without this guard, a persistently-failing nudge
    would loop forever (re-drain the re-injected message, fail again,
    re-inject again). Re-injected messages stay in the mailbox for the
    next /send to consume naturally."""
    meta = msg.get("metadata") or {}
    if meta.get("watcher_reinjected"):
        return False
    if meta.get("cc"):
        return True
    return msg.get("type") in _TRIGGER_TYPES


def _format_nudge(msgs: list[dict]) -> str:
    """Render a drain batch as the watcher's nudge payload.

    The format is a stable, human-readable summary the lead's LLM can
    parse at a glance::

        [Teammate messages]
        - from: alice, type: milestone, cc: False
          content: ...
        - from: bob, type: result, cc: True (originally to: charlie)
          content: ...
    """
    lines = ["[Teammate messages]"]
    for m in msgs:
        meta = m.get("metadata") or {}
        cc = bool(meta.get("cc"))
        suffix = ""
        if cc:
            orig = meta.get("original_to", "?")
            suffix = f" (originally to: {orig})"
        lines.append(
            f"- from: {m.get('from', '?')}, "
            f"type: {m.get('type', '?')}, cc: {cc}{suffix}"
        )
        content = m.get("content", "")
        lines.append(f"  content: {content}")
    return "\n".join(lines)


class LeadWatcher:
    """Daemon thread that polls lead's mailbox and nudges the lead's
    AgentLoop when teammate result/milestone/blocker messages land.

    Construction is cheap; nothing happens until ``start()`` is called.
    ``stop()`` flips a ``threading.Event`` and joins the worker thread,
    so shutdown is prompt and crash-safe.
    """

    def __init__(
        self,
        *,
        bus: "MessageBus",
        project_id: str,
        lead_loop_getter: "Callable[[], AgentLoop | None]",
        poll_interval: float = 1.0,
        debounce: float = 5.0,
        event_persister: "Callable[[dict], None] | None" = None,
    ):
        self.bus = bus
        self.project_id = project_id
        self._lead_loop_getter = lead_loop_getter
        self.poll_interval = poll_interval
        self.debounce = debounce
        self._event_persister = event_persister
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        """Spawn the daemon worker. Idempotent: calling twice is a
        no-op (the second call does nothing while the first thread is
        alive)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(
            target=self._run,
            name=f"LeadWatcher-{self.project_id}",
            daemon=True,
        )
        self._thread = t
        t.start()

    def stop(self, *, join_timeout: float = 5.0) -> None:
        """Signal the worker to exit and join it. Safe to call from any
        thread, including the worker itself (though that's unusual)."""
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=join_timeout)
        self._thread = None

    def is_alive(self) -> bool:
        """True if the worker thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    def rebind(
        self,
        lead_loop_getter: "Callable[[], AgentLoop | None]",
        event_persister: "Callable[[dict], None] | None",
    ) -> None:
        """Swap in a fresh ``lead_loop_getter`` and ``event_persister`` on
        a running watcher. Called by ``TeammateSpawner.start_lead_watcher``
        when the user opens a new session — without this, the FIRST
        /send's closures would win forever and daemon-path events would
        keep being persisted into the FIRST session's events.jsonl even
        after the user moved on.

        Safe to call from any thread; ``_tick`` reads these fields
        without a lock (worst case: one tick uses the old getter before
        the new one takes effect — that's fine, the old session is
        still alive for one more cycle).
        """
        self._lead_loop_getter = lead_loop_getter
        self._event_persister = event_persister

    # ── Worker loop ──────────────────────────────────────────────────
    def _run(self) -> None:
        """Main poll loop. Exits cleanly when ``_stop`` is set."""
        while not self._stop.is_set():
            # Interruptible sleep — stop() during the wait still wakes
            # us immediately.
            if self._stop.wait(self.poll_interval):
                return
            try:
                self._tick()
            except Exception:
                # A transient error (e.g. bus momentarily locked) must
                # not tear down the daemon. Log nothing — the bus and
                # AgentLoop already log on their own paths; the watcher
                # just retries next tick.
                pass

    def _tick(self) -> None:
        """One poll cycle: peek → maybe debounce → maybe drain + nudge."""
        # Peek (non-destructive) so non-triggering batches stay in the
        # mailbox for the lead's own /send path.
        pending = self.bus.peek_inbox("lead")
        if not any(_is_trigger(m) for m in pending):
            return
        # Debounce: wait for the burst to settle. Interruptible so
        # stop() during debounce still exits promptly.
        if self._stop.wait(self.debounce):
            return
        # Resolve the lead loop BEFORE draining. If no lead session is
        # bound, we skip without touching the mailbox — the spec says
        # the watcher should "tick but do nothing, no crash" when the
        # lead isn't around. The mailbox content stays for next /send.
        loop = self._lead_loop_getter()
        if loop is None:
            return
        # Route daemon-path events (from _run_until_idle) to the
        # persister. We install on ``watcher_event_sink`` — a dedicated
        # field, NOT ``on_event`` — so /send-driven turns can't
        # double-write events.jsonl. ``_run_impl`` calls ``_emit``
        # (which fires ``on_event``) for tool_use / tool_result /
        # todos_updated / cron_fired / retry / etc. AND yields those
        # same events; the /send generator already persists via the
        # yield path. If the persister lived on ``on_event``, every
        # tool event during a /send would land on disk twice. The
        # dedicated ``watcher_event_sink`` is only routed from
        # ``_run_until_idle`` (the daemon path), so the /send yield
        # path is untouched. See ``AgentLoop._run_until_idle``.
        if self._event_persister is not None:
            loop.watcher_event_sink = self._event_persister
        # TOCTOU re-check: the lead's own /send may have drained the
        # mailbox while we were debouncing. If so, the user is back and
        # their /send will surface the content — we must NOT nudge.
        drained = self.bus.read_inbox("lead")
        if not drained:
            return
        # Phase I.C.5: emit a single ``lead_nudged`` notice event BEFORE
        # nudging so the frontend can render a gray "Alice reported a
        # milestone → lead is responding..." toast on the bubble the
        # daemon turn is about to drive. Order matters: the notice must
        # be the FIRST persisted event of this drain so it lands on the
        # streaming bubble the daemon-thread turn creates, not on the
        # tail of the previous bubble. Burst-collapsed: one drain → one
        # notice carrying every drained teammate name + kind. Best-effort
        # — if the persister is absent (legacy / unmonitored session) we
        # still nudge, the notice is observability sugar not correctness.
        if self._event_persister is not None:
            try:
                self._event_persister({
                    "type": "lead_nudged",
                    "items": [
                        {
                            "from": m.get("from", "?"),
                            "kind": m.get("type", "message"),
                        }
                        for m in drained
                    ],
                })
            except Exception:
                # The persister is best-effort. Never block the nudge
                # path on event persistence — the user's next /send
                # still surfaces the content via the normal flow.
                pass
        # Nudge while holding the drained batch. If nudge itself raises
        # (loop is shutting down, etc.) we re-inject the messages so
        # the next /send picks them up rather than dropping them. We
        # tag re-injected messages with watcher_reinjected=True so the
        # next tick's trigger check ignores them — otherwise a
        # persistently-failing nudge would loop forever (re-drain →
        # re-inject → re-drain). The re-injected content still reaches
        # the user's next /send via the normal mailbox drain path.
        try:
            loop.nudge(_format_nudge(drained))
        except Exception:
            for m in drained:
                meta = dict(m.get("metadata") or {})
                meta["watcher_reinjected"] = True
                self.bus.send(
                    m.get("from", "teammate"), "lead",
                    m.get("content", ""),
                    msg_type=m.get("type", "message"),
                    metadata=meta,
                )


__all__ = ["LeadWatcher"]
