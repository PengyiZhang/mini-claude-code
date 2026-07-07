"""Channel receiver supervisor — singleton that owns long-lived receiver
threads for ws-mode channel bindings.

Modeled after ``MCPPool`` (per-project connections tracked centrally) so
the shutdown order + observability is consistent. The supervisor is
constructed once per server process and stashed on the FastAPI app
state. All spawn / teardown goes through it:

* ``ProjectManager._assemble`` calls ``start_for`` for each ws binding
  after building the registry.
* HTTP ``POST /channels`` calls ``start_for`` after ``reg.add`` succeeds.
* HTTP ``DELETE /channels/{id}`` calls ``stop_for`` before ``reg.remove``.
* ``ProjectManager.invalidate`` calls ``stop_project`` before dropping
  the cached Project so the next ``get()`` rebuilds + respawns.
* Server lifespan shutdown calls ``stop_all`` after MCP ``disconnect_all``.

Invariant: each ``(tenant_id, project_id, channel_id)`` key has at most
one receiver thread at any moment. ``start_for`` is idempotent — calling
it twice with the same key stops the previous receiver before starting
a new one, so binding edits (config refresh) replace cleanly.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from .base import ChannelBinding, ChannelRegistry
from .inbound import enqueue_inbound_turn

if TYPE_CHECKING:
    from ..projects import Project
    from ..session import SessionManager


log = logging.getLogger("mini_cc.channels.supervisor")


# Type of the inbound callback the channel receiver invokes:
# ``(user_input: str, metadata: dict) -> None``.
OnInbound = Callable[[str, dict], None]


class ChannelReceiverSupervisor:
    """Owns one receiver thread per ws-mode binding. Singleton — created
    once at server startup, stashed on ``app.state`` for reuse."""

    def __init__(self) -> None:
        self._receivers: dict[tuple[str, str, str], object] = {}
        self._lock = threading.RLock()
        # SessionManager reference — set once at lifespan startup so
        # callers (_assemble / HTTP routes / invalidate) don't have to
        # plumb it through. Stays None in unit tests that exercise the
        # supervisor without a live SessionManager.
        self._sm: "SessionManager | None" = None

    def attach_session_manager(self, sm: "SessionManager") -> None:
        """Bind the SessionManager that ``start_for`` will use to inject
        inbound messages. Called once during server lifespan startup."""
        self._sm = sm

    def start_for(self, project: "Project",
                  binding: ChannelBinding) -> bool:
        """Build a Channel for ``binding`` and start its receiver thread.

        Returns ``True`` if a receiver was actually started, ``False``
        when the kind doesn't support ws (``supported_transports`` doesn't
        include it) or the channel instance lacks ``start_receiver``.
        Either way, no exception bubbles up — a broken binding shouldn't
        stall project assembly or HTTP create.

        Idempotent: re-entrant calls with the same key first stop the
        existing receiver, then start a fresh one (so binding edits that
        change credentials get a clean restart)."""
        if getattr(binding, "transport", "webhook") != "ws":
            return False
        if self._sm is None:
            # No SessionManager bound — happens in unit tests + when
            # SDK mode is off. Skip rather than raise so callers stay
            # cheap to invoke.
            log.debug("ws binding %s: no SessionManager — skipped",
                      binding.id)
            return False
        reg: ChannelRegistry | None = getattr(project, "channels", None)
        if reg is None:
            return False
        channel = reg.get_channel(binding)
        if channel is None:
            log.warning(
                "ws binding %s: kind %s has no factory — receiver skipped",
                binding.id, binding.kind)
            return False
        starter = getattr(channel, "start_receiver", None)
        if not callable(starter):
            log.warning(
                "ws binding %s: kind %s channel lacks start_receiver — "
                "skipped", binding.id, binding.kind)
            return False

        key = (project.meta.tenant_id, project.project_id, binding.id)
        on_inbound = self._build_on_inbound(project, binding, self._sm)
        with self._lock:
            existing = self._receivers.get(key)
            if existing is not None:
                # Idempotent restart: stop the previous receiver before
                # spawning the new one so we never have two threads for
                # one binding.
                self._stop_unlocked(key, existing)
            try:
                starter(on_inbound)
            except Exception:
                log.exception("ws binding %s: start_receiver failed",
                              binding.id)
                return False
            self._receivers[key] = channel
        log.info("ws binding %s: receiver started", binding.id)
        return True

    def stop_for(self, tenant_id: str, project_id: str,
                 channel_id: str) -> None:
        """Stop the receiver for a single binding. No-op if unknown."""
        key = (tenant_id, project_id, channel_id)
        with self._lock:
            existing = self._receivers.pop(key, None)
        if existing is None:
            return
        self._stop_unlocked(key, existing)

    def stop_project(self, tenant_id: str, project_id: str) -> None:
        """Stop every receiver belonging to one project. Called by
        ``ProjectManager.invalidate`` before dropping the cached Project
        so the next ``get()`` cleanly rebuilds + respawns."""
        with self._lock:
            keys = [k for k in self._receivers
                    if k[0] == tenant_id and k[1] == project_id]
            entries = [(k, self._receivers.pop(k)) for k in keys]
        for key, channel in entries:
            self._stop_unlocked(key, channel)

    def stop_all(self) -> None:
        """Stop every receiver. Called by lifespan shutdown."""
        with self._lock:
            entries = list(self._receivers.items())
            self._receivers.clear()
        for _key, channel in entries:
            try:
                stopper = getattr(channel, "stop_receiver", None)
                if callable(stopper):
                    stopper()
            except Exception:
                log.exception("stop_receiver failed during stop_all")

    def status(self) -> list[dict]:
        """Snapshot of active receivers — for diagnostics / future UI."""
        with self._lock:
            out = []
            for (tid, pid, cid), channel in self._receivers.items():
                running = bool(getattr(channel, "receiver_running", False))
                out.append({
                    "tenant_id": tid, "project_id": pid,
                    "channel_id": cid, "running": running,
                })
            return out

    # ── Internals ───────────────────────────────────────────────────

    def _build_on_inbound(self, project: "Project",
                          binding: ChannelBinding,
                          sm: "SessionManager") -> OnInbound:
        """Build the callback the receiver invokes for each event. Closes
        over the bound session_id + project so the receiver doesn't need
        any context beyond (user_input, metadata)."""
        def _on_inbound(user_input: str, metadata: dict) -> None:
            enqueue_inbound_turn(sm, project, binding.session_id,
                                 user_input, metadata)
        return _on_inbound

    def _stop_unlocked(self, key, channel) -> None:
        try:
            stopper = getattr(channel, "stop_receiver", None)
            if callable(stopper):
                stopper()
        except Exception:
            log.exception("stop_receiver failed for %s", key)


# ── Module-level singleton ────────────────────────────────────────────
# Process-wide supervisor. Constructed lazily on first access — both
# production lifespan and unit tests share the same accessor. Use
# ``reset_for_test()`` in tests that want a fresh state.

_supervisor: "ChannelReceiverSupervisor | None" = None
_supervisor_lock = threading.Lock()


def get_supervisor() -> ChannelReceiverSupervisor:
    """Return the process-wide supervisor instance, creating it on first
    call. Safe to call from any thread."""
    global _supervisor
    if _supervisor is None:
        with _supervisor_lock:
            if _supervisor is None:
                _supervisor = ChannelReceiverSupervisor()
    return _supervisor


def reset_for_test() -> ChannelReceiverSupervisor:
    """Replace the singleton with a fresh instance. Test-only — used by
    tests that want a clean supervisor state without cross-test leakage.
    Also clears the feishu_common inbound-chat-id cache so deliver()
    fallback state doesn't leak across tests that reuse the same
    binding.id."""
    global _supervisor
    with _supervisor_lock:
        if _supervisor is not None:
            _supervisor.stop_all()
        _supervisor = ChannelReceiverSupervisor()
    # Clear shared inbound chat-id cache (defined in feishu_common —
    # lazy import to avoid a circular dependency at module load).
    try:
        from . import feishu_common
        feishu_common._INBOUND_CHAT_IDS.clear()
    except Exception:
        pass
    return _supervisor


__all__ = ["ChannelReceiverSupervisor",
           "get_supervisor", "reset_for_test"]
