"""Feishu (Lark) bidirectional channel — **WebSocket long-connection mode**.

Companion to ``feishu.py`` (webhook mode). Uses the official
``lark-oapi`` Python SDK which handles handshake, ack, and automatic
reconnect. Unlike webhook mode this requires **no public URL** — the
process opens an outbound ``wss://`` to Feishu and Feishu pushes events
down it.

Why a separate file from ``feishu.py``:
- Webhook mode uses pure stdlib + PyCryptodome (zero SDK dep, matches
  mini_cc's "stdlib-only" philosophy).
- WS mode requires ``lark-oapi`` (the SDK owns the WS protocol). Putting
  the SDK import in this file means installations without WS bindings
  never pay the import cost.

SDK limitation
--------------
``lark.ws.Client.start()`` has no public ``stop()`` API — the SDK owns
the connection lifecycle. ``stop_receiver`` only sets a ``threading.Event``
so the next reconnect loop iteration sees it and bails. The currently-
active connection can take up to ~5s to wind down, and in pathological
cases the daemon thread will only be reclaimed at process exit. This is
a known SDK limitation; document it in operator-facing README and rely
on ``daemon=True`` for the "kill -9" path.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from .base import Channel, ChannelBinding, InboundResult
from .feishu_common import (
    TokenCache,
    _FEISHU_OPEN_BASE,
    lookup_inbound_chat_id,
    parse_message_event,
    remember_inbound_chat_id,
    render_event,
)

log = logging.getLogger("mini_cc.channels.feishu_ws")

# lark-oapi is heavy + optional — only import when WS bindings actually
# need it. Failures here are surfaced via ``ImportError`` at first
# ``start_receiver`` call.
try:
    import lark_oapi as lark  # type: ignore
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # type: ignore
    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    P2ImMessageReceiveV1 = None  # type: ignore
    _HAS_LARK = False


OnInbound = Callable[[str, dict], None]


class FeishuWsChannel:
    """WS long-connection Feishu channel. Implements the optional
    receiver lifecycle methods (``start_receiver`` / ``stop_receiver`` /
    ``receiver_running``) — webhook-specific ``handle_inbound`` is
    inherited as a no-op so a ws-mode binding never accepts inbound via
    the public webhook URL (it stays WS-only)."""

    kind = "feishu"
    supported_transports = ("ws",)

    def __init__(self, binding: ChannelBinding):
        self.binding = binding
        cfg = binding.config or {}
        self.app_id = str(cfg.get("app_id", ""))
        self.app_secret = str(cfg.get("app_secret", ""))
        self.chat_id = str(cfg.get("chat_id", "") or "")
        # Token cache shared with the webhook impl so deliver() works
        # exactly the same way (outbound is transport-independent).
        self._token_cache = TokenCache(self.app_id, self.app_secret)
        # Receiver state.
        self._client: "object | None" = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_inbound: OnInbound | None = None
        self._lock = threading.Lock()
        # Last-seen inbound chat_id — used as outbound target when
        # config.chat_id is empty. Operators usually don't know the
        # oc_xxx upfront; they just want replies to go back to whichever
        # chat the question came from.
        self._last_chat_id: str | None = None

    # ── Receiver lifecycle (called by ChannelReceiverSupervisor) ─────

    def start_receiver(self, on_inbound: OnInbound) -> None:
        """Spawn the WS client thread. Idempotent across stop/start —
        ``_stop`` is cleared here in case ``stop_receiver`` was called
        previously on the same instance."""
        if not _HAS_LARK:
            raise RuntimeError(
                "lark-oapi SDK not installed — run "
                "`pip install lark-oapi` to use ws transport")
        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "feishu ws binding requires app_id + app_secret")
        self._on_inbound = on_inbound
        self._stop.clear()

        dispatcher = self._build_dispatcher()
        self._client = lark.ws.Client(  # type: ignore[union-attr]
            self.app_id,
            self.app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.INFO,  # type: ignore[union-attr]
        )
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"feishu-ws:{self.binding.id}")
        self._thread.start()

    def stop_receiver(self) -> None:
        """Signal the WS loop to bail. The active connection may take
        up to ~5s to actually close (SDK owns the socket)."""
        self._stop.set()

    @property
    def receiver_running(self) -> bool:
        return (self._thread is not None
                and self._thread.is_alive()
                and not self._stop.is_set())

    # ── Internal ────────────────────────────────────────────────────

    def _build_dispatcher(self):
        """Construct the SDK event dispatcher. Maps each event type to
        a method on this channel instance so we can evolve handlers
        independently of the dispatcher wiring."""
        # Verification token / encrypt key aren't consumed here — the
        # SDK does signature verification implicitly via the WS channel.
        builder = lark.EventDispatcherHandler.builder("", "")  # type: ignore[union-attr]
        return (
            builder
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

    def _on_message_receive(self, event) -> None:
        """SDK callback for ``im.message.receive_v1``. Converts the SDK
        event object into our ``(user_input, metadata)`` shape and
        invokes the supervisor-provided callback."""
        try:
            # SDK event wraps the same JSON shape Feishu sends via
            # webhook. ``event.event`` is the inner ``event`` dict
            # (sender / message / etc.); ``event.header`` has event_id
            # + token. Reuse the webhook-mode parser so @-mention
            # stripping + non-text fallback stay identical.
            inner = getattr(event, "event", None)
            if inner is None:
                return
            # Convert SDK domain object to plain dict for the shared
            # parser. lark-oapi events expose ``__dict__`` / are JSON-
            # serializable via the SDK's helper.
            event_dict = _sdk_event_to_dict(inner)
            text, metadata = parse_message_event(event_dict)
            if text is None or self._on_inbound is None:
                return
            # M3-3 spirit: ws-mode receives leave no HTTP trace, so log
            # enough to debug "did the message even arrive" without a
            # console round-trip.
            log.info("feishu ws inbound: binding=%s chat=%s text=%r",
                     self.binding.id, metadata.get("chat_id"),
                     text[:80])
            # Remember the chat_id so deliver() can reply even when the
            # binding wasn't configured with one (common: operators add
            # the bot to a chat and start typing without knowing oc_xxx).
            # Stored in the module-level shared cache because the
            # dispatcher builds a fresh Channel instance per outbound
            # deliver — instance state would be lost.
            cid = metadata.get("chat_id")
            if cid:
                self._last_chat_id = cid
                remember_inbound_chat_id(self.binding.id, cid)
            try:
                self._on_inbound(text, metadata)
            except Exception:
                log.exception("on_inbound callback raised for binding %s",
                              self.binding.id)
        except Exception:
            log.exception("feishu ws event handling failed for binding %s",
                          self.binding.id)

    def _run(self) -> None:
        """Worker thread: drive the SDK's blocking ``start()`` call until
        ``_stop`` is set. SDK has no graceful stop, so we rely on
        ``_stop`` being checked between reconnect attempts."""
        # The SDK's ``start()`` itself retries internally on transient
        # failures; if it ever returns / raises, we honor ``_stop`` and
        # otherwise sleep briefly and re-enter. ``daemon=True`` ensures
        # process exit doesn't block on a stuck SDK call.
        #
        # Event-loop fix: ``lark_oapi/ws/client.py:32`` captures
        # ``asyncio.get_event_loop()`` at module import time. Under
        # FastAPI/uvicorn that import happens *after* the server's main
        # loop is already running, so the captured loop is the main
        # thread's running loop — and calling
        # ``loop.run_until_complete()`` on it from this worker thread
        # raises "This event loop is already running". Surgical fix:
        # override the SDK's module-level ``loop`` with a fresh one bound
        # to this thread before calling ``start()``. The SDK reads
        # ``loop`` dynamically, so this is sufficient.
        import asyncio
        import sys
        ws_client_mod = sys.modules.get("lark_oapi.ws.client")
        if ws_client_mod is None:
            log.warning("lark_oapi.ws.client not in sys.modules; "
                        "skipping event-loop reset (SDK may fail with "
                        "'This event loop is already running')")
        else:
            try:
                fresh = asyncio.new_event_loop()
                asyncio.set_event_loop(fresh)
                ws_client_mod.loop = fresh  # type: ignore[attr-defined]
            except Exception:
                log.exception("could not reset lark ws event loop; aborting")
                return
        bound_loop = ws_client_mod.loop if ws_client_mod is not None else None
        try:
            while not self._stop.is_set():
                try:
                    self._client.start()  # type: ignore[union-attr]
                except Exception:
                    log.exception("feishu ws client for binding %s raised; "
                                  "retry in 5s", self.binding.id)
                    if self._stop.wait(5.0):
                        return
                else:
                    # ``start()`` returning normally is unusual (it's a
                    # blocking call); treat as a transient exit and loop.
                    if self._stop.wait(1.0):
                        return
        finally:
            # Clean shutdown: the SDK schedules background asyncio tasks
            # on our fresh loop (e.g. ExpiringCache._start_clear_cron at
            # lark_oapi/core/cache/expiring_cache.py:44). Abandoning the
            # loop with pending tasks triggers "Task was destroyed but it
            # is pending!" warnings on thread exit. Cancel + gather + close.
            self._cleanup_loop(bound_loop)

    @staticmethod
    def _cleanup_loop(loop) -> None:
        if loop is None or loop.is_closed():
            return
        try:
            pending = list(asyncio.all_tasks(loop))
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass

    # ── Webhook-only methods (no-op for WS) ─────────────────────────

    def handle_inbound(self, body: bytes,
                       headers: dict[str, str]) -> InboundResult:
        """WS-mode bindings don't expose a webhook URL by default — but
        the public webhook endpoint still resolves them, so we honor
        ``url_verification`` (for setup-time misuse) and otherwise no-op.
        Operators who want both transports should configure two bindings."""
        try:
            import json
            outer = json.loads(body.decode("utf-8"))
        except Exception:
            return InboundResult()
        if isinstance(outer, dict) and outer.get("type") == "url_verification":
            return InboundResult(
                verification_response={"challenge": outer.get("challenge", "")})
        return InboundResult()

    def deliver(self, event: dict) -> None:
        """Outbound push is identical to webhook mode (transport-
        independent — uses the same REST endpoint + token cache).

        Target chat resolves to ``config.chat_id`` if set, else falls
        back to the most recently seen inbound chat_id (shared cache).
        Empty fallback logs a warning once per deliver (operators get a
        clear signal rather than silent no-op)."""
        target = self.chat_id or self._last_chat_id \
            or lookup_inbound_chat_id(self.binding.id)
        if not target:
            log.warning(
                "feishu ws binding %s deliver skipped: no chat_id "
                "(config empty AND no inbound seen yet) — set chat_id "
                "in binding config or wait for an inbound message",
                self.binding.id)
            return
        text = render_event(event)
        if not text:
            return
        token = self._token_cache.get()
        if not token:
            log.warning(
                "feishu ws binding %s deliver skipped: no tenant token "
                "(app_id/app_secret missing or wrong)",
                self.binding.id)
            return
        # Reuse the same outbound HTTP path as webhook mode — keeps
        # rendering + token logic in one place.
        try:
            import json as _json
            import requests  # type: ignore
            content = _json.dumps({"text": text}, ensure_ascii=False)
            resp = requests.post(
                f"{_FEISHU_OPEN_BASE}/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": target,
                    "msg_type": "text",
                    "content": content,
                },
                timeout=5.0,
            )
            log.info(
                "feishu ws binding %s deliver to %s: HTTP %s",
                self.binding.id, target, resp.status_code)
        except Exception:
            log.exception(
                "feishu ws binding %s deliver HTTP request failed",
                self.binding.id)


def _sdk_event_to_dict(obj) -> dict:
    """Best-effort conversion of a lark-oapi SDK event object to the
    plain dict our shared parser expects. lark-oapi domain classes
    expose a ``__dict__`` populated with nested domain objects, so we
    walk one level deep. Falls back to ``{}`` on any failure — the
    caller treats empty as 'drop'."""
    try:
        # lark-oapi Python SDK domain objects have a mappable accessor.
        # When the SDK is missing or the event shape drifts, fall back
        # to walking __dict__ + nested __dict__.
        out: dict = {}
        for k, v in vars(obj).items():
            if hasattr(v, "__dict__"):
                inner = {}
                for ik, iv in vars(v).items():
                    inner[ik] = _value_to_plain(iv)
                out[k] = inner
            else:
                out[k] = _value_to_plain(v)
        return out
    except Exception:
        return {}


def _value_to_plain(v):
    if hasattr(v, "__dict__"):
        return {ik: _value_to_plain(iv)
                for ik, iv in vars(v).items()}
    if isinstance(v, (list, tuple)):
        return [_value_to_plain(x) for x in v]
    return v


__all__ = ["FeishuWsChannel"]
