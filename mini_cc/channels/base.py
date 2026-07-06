"""Bidirectional channel abstraction.

A ``Channel`` is an external transport binding (Feishu group, Slack
channel, Discord guild, …) that flows messages INTO mini_cc and receives
events OUT of mini_cc. This module defines the protocol every channel
implementation must satisfy, the per-project registry that persists
bindings, and the small dataclasses that flow between the HTTP layer
and the channel implementations.

Design split
------------
* ``ChannelBinding`` — the persisted record: id, kind, opaque config
  dict, optional bound lead session, outbound event-type filter.
* ``Channel`` protocol — the runtime contract: ``handle_inbound`` for
  inbound webhooks + ``deliver`` for outbound fan-out. Implementations
  own their HTTP/signature/token concerns.
* ``ChannelRegistry`` — per-project, persisted at
  ``<state_root>/<project_id>/channels.json``. Mirrors the
  ``WebhookRegistry`` shape so the HTTP routes and the live dispatcher
  share one in-memory object.

Why a separate layer from ``WebhookRegistry``?
----------------------------------------------
WebhookRegistry is one-way OUT (project event → external URL). Channels
are bidirectional and need:
- Per-kind request parsing (Feishu's url_verification challenge, Slack's
  signed payload, etc.).
- Per-kind credential management (Feishu app_id+app_secret for outbound
  token refresh).
- Inbound routing — the external service calls our webhook; we must
  resolve the binding, parse the payload, and inject it into the bound
  session.

Coupling those concerns into the existing one-way webhook dispatcher
would force it to know about every transport's quirks. A dedicated
channel layer keeps each transport's logic in one file
(``channels/feishu.py``, future ``channels/slack.py``, …).
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable


# ── Dataclasses flowing through the layer ────────────────────────────────

@dataclass
class InboundResult:
    """What a channel produced from one inbound HTTP request.

    Three cases (mutually exclusive in practice):

    * ``verification_response`` is set — the inbound was a setup-time
      handshake ping (Feishu ``url_verification``, Slack ``url_verification``,
      etc.). The HTTP layer should respond with this exact JSON body
      instead of running the agent loop.

    * ``user_input`` is set — the inbound carried a real user message
      that should be injected into the bound session as a new turn.

    * Both ``None`` — the inbound was a no-op (duplicate event, ignored
      event_type, etc.). The HTTP layer responds 200 OK with empty body.
    """
    verification_response: dict | None = None
    user_input: str | None = None
    # Optional sender / origin metadata, surfaced to the agent context.
    metadata: dict = field(default_factory=dict)


@dataclass
class ChannelBinding:
    """Persisted record of one channel binding.

    ``config`` is opaque per kind — Feishu stores ``app_id`` /
    ``app_secret`` / ``encrypt_key`` / ``verification_token`` /
    ``chat_id``; future channels store their own shape. Kept as a
    free-form dict so the registry doesn't have to know about each
    transport's schema.
    """
    id: str
    kind: str
    config: dict
    # Lead session the channel routes inbound into. None = project's
    # default session (resolved by the HTTP layer via SessionManager).
    session_id: str | None = None
    # Outbound filter; empty list subscribes to all event types.
    event_types: list[str] = field(default_factory=list)
    created_at: str = ""


# ── Channel protocol ────────────────────────────────────────────────────

@runtime_checkable
class Channel(Protocol):
    """Runtime contract for a channel implementation.

    Implementations are constructed by ``ChannelRegistry.get_channel``
    with the binding's config dict; they live in-memory only (not
    persisted) so token caches / signature state are reset on registry
    reload.
    """

    kind: str

    def handle_inbound(self, body: bytes,
                       headers: dict[str, str]) -> InboundResult:
        """Parse + verify an inbound webhook. Returns an InboundResult
        describing what (if anything) should be injected into the bound
        session, or echoed back as a verification handshake."""
        ...

    def deliver(self, event: dict) -> None:
        """Push a session/teammate event to the external service.

        Best-effort: raise on persistent failures; the dispatcher wraps
        each call in try/except so a broken channel can't stall the
        event fan-out path."""
        ...


# ── Registry ────────────────────────────────────────────────────────────

class ChannelRegistry:
    """Project-scoped registry of channel bindings.

    Persists to ``<state_root>/<project_id>/channels.json``. Thread-safe
    via a single RLock — write rate is operator-driven, not hot. The
    HTTP routes and the live dispatcher share one in-memory object so a
    binding added via HTTP is visible to the next event without
    re-warming the session.
    """

    FILENAME = "channels.json"

    def __init__(self, state_root: Path, project_id: str):
        self._root = Path(state_root) / project_id
        self._root.mkdir(parents=True, exist_ok=True)
        self._fp = self._root / self.FILENAME
        self._lock = threading.RLock()
        self._bindings: dict[str, ChannelBinding] = self._load()

    def _load(self) -> dict[str, ChannelBinding]:
        if not self._fp.is_file():
            return {}
        try:
            raw = json.loads(self._fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        out: dict[str, ChannelBinding] = {}
        for rec in raw if isinstance(raw, list) else []:
            try:
                out[rec["id"]] = ChannelBinding(
                    id=rec["id"],
                    kind=rec["kind"],
                    config=dict(rec.get("config") or {}),
                    session_id=rec.get("session_id"),
                    event_types=list(rec.get("event_types") or []),
                    created_at=rec.get("created_at", ""),
                )
            except (KeyError, TypeError):
                continue
        return out

    def _persist_locked(self) -> None:
        payload = [
            {
                "id": b.id,
                "kind": b.kind,
                "config": dict(b.config),
                "session_id": b.session_id,
                "event_types": list(b.event_types),
                "created_at": b.created_at,
            }
            for b in self._bindings.values()
        ]
        tmp = self._fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._fp)

    def list(self) -> list[ChannelBinding]:
        with self._lock:
            return list(self._bindings.values())

    def get(self, channel_id: str) -> ChannelBinding | None:
        with self._lock:
            return self._bindings.get(channel_id)

    def add(self, kind: str, config: dict,
            *, session_id: str | None = None,
            event_types: Iterable[str] = ()) -> ChannelBinding:
        with self._lock:
            binding = ChannelBinding(
                id=f"chan_{uuid.uuid4().hex[:12]}",
                kind=kind,
                config=dict(config),
                session_id=session_id,
                event_types=[t for t in event_types if t],
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
            )
            self._bindings[binding.id] = binding
            self._persist_locked()
            return binding

    def update(self, channel_id: str, *,
               config: dict | None = None,
               session_id: str | None | "UNSET" = "UNSET",  # type: ignore[assignment]
               event_types: list[str] | None = None) -> ChannelBinding | None:
        """Patch a binding in place. ``session_id`` accepts None to
        rebind to project default; pass the literal ``"UNSET"`` sentinel
        (default) to leave the field untouched."""
        with self._lock:
            b = self._bindings.get(channel_id)
            if b is None:
                return None
            if config is not None:
                b.config = dict(config)
            if session_id != "UNSET":
                b.session_id = session_id  # type: ignore[assignment]
            if event_types is not None:
                b.event_types = list(event_types)
            self._persist_locked()
            return b

    def remove(self, channel_id: str) -> bool:
        with self._lock:
            if channel_id not in self._bindings:
                return False
            del self._bindings[channel_id]
            self._persist_locked()
            return True

    def matches_event(self, event_type: str) -> list[ChannelBinding]:
        """Bindings whose event-type filter admits ``event_type``. Empty
        ``event_types`` on a binding means 'subscribe to all'."""
        with self._lock:
            return [b for b in self._bindings.values()
                    if not b.event_types or event_type in b.event_types]

    def get_channel(self, binding: ChannelBinding) -> Channel | None:
        """Build a Channel instance for ``binding`` by consulting the
        per-kind factory registry. Returns None if no factory has been
        registered for that kind — the caller treats this as 'skip this
        binding' rather than raising so a single broken/unsupported kind
        doesn't stall the whole fan-out."""
        factory = _CHANNEL_KINDS.get(binding.kind)
        if factory is None:
            return None
        try:
            return factory(binding)
        except Exception:
            return None


# ── Kind registry ───────────────────────────────────────────────────────

# Per-kind factory: build a Channel instance from a ChannelBinding.
# Implementations register themselves at import time
# (e.g. ``mini_cc.channels.feishu`` calls ``register_channel_kind`` in its
# module body). Lazy-imported by the server so unused channels impose
# zero startup cost.
_CHANNEL_KINDS: dict[str, Callable[[ChannelBinding], Channel]] = {}


def register_channel_kind(kind: str,
                          factory: Callable[[ChannelBinding], Channel]) -> None:
    """Register a factory for a channel kind. Idempotent: re-registering
    the same kind overwrites the previous factory, which is convenient
    for tests that swap in a fake implementation."""
    _CHANNEL_KINDS[kind] = factory


def registered_kinds() -> list[str]:
    """Sorted list of registered channel kinds — for diagnostics / UI."""
    return sorted(_CHANNEL_KINDS.keys())


# ── Channel dispatcher (outbound fan-out) ────────────────────────────────

class ChannelDispatcher:
    """Fan-out wrapper around a session's ``on_event`` callback that
    mirrors ``WebhookDispatcher`` but pushes to all bound channels
    instead of plain HTTP webhook URLs.

    Construction is cheap; the inner ``on_event`` is called
    synchronously, then each matching channel gets its own background
    delivery thread so one slow channel can't delay another.
    """

    def __init__(self, registry: ChannelRegistry,
                 project_id: str,
                 session_id: str,
                 inner: Callable[[dict], None] | None = None,
                 channel_factory: Callable[[ChannelBinding], Channel] | None = None):
        self._registry = registry
        self._project_id = project_id
        self._session_id = session_id
        self._inner = inner
        # Injectable for tests; defaults to the registry's lazy builder.
        self._channel_factory = channel_factory or registry.get_channel

    def __call__(self, event: dict) -> None:
        if self._inner is not None:
            self._inner(event)
        self._dispatch(event)

    def _dispatch(self, event: dict) -> None:
        etype = str(event.get("type", ""))
        if not etype:
            return
        targets = self._registry.matches_event(etype)
        if not targets:
            return
        for binding in targets:
            try:
                channel = self._channel_factory(binding)
            except Exception:
                continue
            if channel is None:
                continue
            t = threading.Thread(
                target=self._safe_deliver,
                args=(channel, event),
                daemon=True,
            )
            t.start()

    def _safe_deliver(self, channel: Channel, event: dict) -> None:
        try:
            channel.deliver(event)
        except Exception:
            pass


__all__ = [
    "Channel",
    "ChannelBinding",
    "ChannelRegistry",
    "ChannelDispatcher",
    "InboundResult",
]
