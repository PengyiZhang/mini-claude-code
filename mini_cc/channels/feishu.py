"""Feishu (Lark) bidirectional channel.

Inbound
-------
Feishu Open Platform sends events to a configured webhook URL. Setup
handshake is ``{"type":"url_verification","challenge":"<str>","token":
"<str>"}`` → respond ``200 OK`` with body ``{"challenge":"<str>"}``.

Once subscribed, the im.message.receive_v1 event fires for each chat
message. Header ``X-Lark-Signature`` = sha256_hex(timestamp + nonce +
encrypt_key + raw_body). If ``encrypt_key`` is configured, the body is
``{"encrypt":"<base64 AES-256-CBC ciphertext>"}`` and must be decrypted
before parsing.

Outbound
--------
Push to Feishu by POSTing to ``/open-apis/im/v1/messages`` with a
tenant_access_token (refreshed hourly). We cache the token on the
channel instance under a threading.Lock so concurrent dispatchers don't
race the refresh.

Config shape (persisted in ChannelBinding.config)
-------------------------------------------------
``app_id``        str   Feishu custom app id
``app_secret``    str   Feishu custom app secret
``encrypt_key``   str   Optional — required when app configured for
                       encrypted mode (AES key from Feishu console).
``verification_token`` str   Optional — checked against the inbound
                              payload's ``token`` field as a secondary
                              defense.
``chat_id``       str   Target chat for outbound delivery. Required for
                       outbound; inbound parsing doesn't need it.

Why no SDK
----------
Feishu's official Python SDK adds a heavy transitive dep tree and a
twisted-style async API. The HTTP surface is two endpoints and a
SHA256 — pulling in the SDK would be more code than the implementation
itself. We use ``requests`` (already a hard dep elsewhere) for outbound
and the stdlib ``hashlib``/``hmac`` for verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import threading
from typing import Any

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False

from .base import (
    Channel,
    ChannelBinding,
    InboundResult,
    register_channel_kind,
)
from .feishu_common import (
    TokenCache,
    _FEISHU_OPEN_BASE,
    _HTTP_TIMEOUT,
    lookup_inbound_chat_id,
    parse_message_event,
    remember_inbound_chat_id,
    render_event,
    strip_bot_mention,
)


# AES-256-CBC decrypt for the encrypted envelope. Implemented via the
# stdlib hashlib + a vendored CBC routine to avoid pulling in PyCryptodome
# for what is one tiny use case. Falls back to None on any error, which
# the caller treats as "decryption unavailable — skip".
def _aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes | None:
    """Tiny AES-256-CBC decrypt using the stdlib only. Returns None on
    any failure (wrong key length, malformed padding, etc.) so the
    caller can fall through to "skip" rather than raise.

    Implementation note: we use PyCryptodome when available since the
    hashlib AES implementation is one-shot and doesn't expose CBC mode
    directly. If PyCryptodome isn't installed, we surface None — better
    to log "decryption unavailable" than to ship a hand-rolled AES."""
    try:
        from Crypto.Cipher import AES  # type: ignore
        from Crypto.Util.Padding import unpad  # type: ignore
    except ImportError:
        return None
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plain = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return plain
    except Exception:
        return None


def _decrypt_feishu_envelope(encrypt_key: str,
                             encrypt_field: str) -> str | None:
    """Decrypt the ``{"encrypt":"..."}`` body Feishu sends in encrypted
    mode. Returns the inner JSON as a string, or None on any failure.

    Format reference (Feishu docs):
        key    = SHA256(encrypt_key)  (32 bytes)
        ciphertext = base64decode(encrypt_field)
        iv     = first 16 bytes of ciphertext
        data   = rest of ciphertext, AES-256-CBC, PKCS7-padded
    """
    if not encrypt_key or not encrypt_field:
        return None
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    try:
        raw = base64.b64decode(encrypt_field)
    except Exception:
        return None
    if len(raw) < 16:
        return None
    iv = raw[:16]
    ciphertext = raw[16:]
    plain = _aes_cbc_decrypt(key, iv, ciphertext)
    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _signature(timestamp: str, nonce: str, encrypt_key: str,
               body: bytes) -> str:
    """X-Lark-Signature = sha256_hex(timestamp + nonce + encrypt_key + body)."""
    s = f"{timestamp}{nonce}{encrypt_key}".encode("utf-8") + body
    return hashlib.sha256(s).hexdigest()


def _verify_signature(headers: dict[str, str], encrypt_key: str,
                      body: bytes) -> bool:
    """Constant-time compare of the inbound X-Lark-Signature. Returns
    False when the header is missing or the signature doesn't match."""
    sig = headers.get("X-Lark-Signature") or headers.get("x-lark-signature")
    if not sig:
        return False
    ts = headers.get("X-Lark-Request-Timestamp") or headers.get(
        "x-lark-request-timestamp") or ""
    nonce = headers.get("X-Lark-Request-Nonce") or headers.get(
        "x-lark-request-nonce") or ""
    expected = _signature(ts, nonce, encrypt_key, body)
    return _const_time_eq(sig, expected)


def _const_time_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    out = 0
    for x, y in zip(a, b):
        out |= ord(x) ^ ord(y)
    return out == 0


class FeishuChannel:
    """Feishu (Lark) bidirectional channel. Implements both
    ``handle_inbound`` (webhook receiver) and ``deliver`` (outbound push).

    Token refresh is delegated to ``feishu_common.TokenCache``.
    """

    kind = "feishu"
    # Webhook mode is the long-shipped default; WS mode is implemented
    # in ``feishu_ws.py`` and dispatched via a separate factory that
    # supports both. This class is the webhook implementation.
    supported_transports = ("ws", "webhook")

    def __init__(self, binding: ChannelBinding):
        self.binding = binding
        cfg = binding.config or {}
        self.app_id = str(cfg.get("app_id", ""))
        self.app_secret = str(cfg.get("app_secret", ""))
        self.encrypt_key = str(cfg.get("encrypt_key", "") or "")
        self.verification_token = str(cfg.get("verification_token", "") or "")
        self.chat_id = str(cfg.get("chat_id", "") or "")
        # Override base URL for testing / on-prem deployments.
        self.open_base = str(cfg.get("open_base", _FEISHU_OPEN_BASE))
        self._token_cache = TokenCache(self.app_id, self.app_secret,
                                       open_base=self.open_base)
        # Last-seen inbound chat_id — outbound target fallback when
        # config.chat_id is empty. Mirrors FeishuWsChannel so operators
        # can leave chat_id blank in both transports.
        self._last_chat_id: str | None = None
        # Module-level logger (feishu.py has no `log` defined).
        import logging as _logging
        self._log = _logging.getLogger("mini_cc.channels.feishu")

    @property
    def verification_configured(self) -> bool:
        """M2-6: without encrypt_key (signature) or verification_token
        (per-event token), any forged payload would be accepted and
        trigger a paid LLM turn. The inbound route refuses such
        channels outright."""
        return bool(self.encrypt_key or self.verification_token)

    # ── Inbound ──────────────────────────────────────────────────────

    def handle_inbound(self, body: bytes,
                       headers: dict[str, str]) -> InboundResult:
        # If encrypt_key is configured, signature must verify. If it
        # isn't configured, signature header won't be present — skip
        # the check (matches Feishu's plain mode).
        if self.encrypt_key:
            if not _verify_signature(headers, self.encrypt_key, body):
                return InboundResult()

        # Decrypt envelope first if needed.
        try:
            outer = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return InboundResult()
        if isinstance(outer, dict) and "encrypt" in outer:
            plain = _decrypt_feishu_envelope(self.encrypt_key,
                                             str(outer["encrypt"]))
            if plain is None:
                return InboundResult()
            try:
                payload = json.loads(plain)
            except json.JSONDecodeError:
                return InboundResult()
        else:
            payload = outer

        if not isinstance(payload, dict):
            return InboundResult()

        # URL verification handshake — happens once at webhook setup.
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            return InboundResult(verification_response={"challenge": challenge})

        # Secondary token check (Feishu's per-event token in the header).
        if self.verification_token:
            header = payload.get("header") or {}
            if header.get("token") != self.verification_token:
                return InboundResult()

        event_type = (payload.get("header") or {}).get("event_type", "")
        if event_type != "im.message.receive_v1":
            return InboundResult()

        text, metadata = parse_message_event(payload.get("event") or {})
        if text is None:
            return InboundResult()
        # Remember chat_id for outbound fallback (mirrors FeishuWsChannel).
        # Stored in module-level shared cache so deliver() — which runs
        # on a freshly-built Channel instance — can read it.
        cid = metadata.get("chat_id")
        if cid:
            self._last_chat_id = cid
            remember_inbound_chat_id(self.binding.id, cid)
        return InboundResult(user_input=text, metadata=metadata)

    # ── Outbound ─────────────────────────────────────────────────────

    def deliver(self, event: dict) -> None:
        """Push a session event to Feishu. Renders text-bearing events
        (text / teammate_message / lead_nudged / tool_result) as Feishu
        text messages; other event types are ignored to avoid spamming
        the chat with per-tool-call noise.

        Target resolves to ``config.chat_id`` if set, else the most
        recently seen inbound chat_id. Empty fallback logs a warning
        rather than silent no-op (operators get a clear signal)."""
        target = self.chat_id or self._last_chat_id \
            or lookup_inbound_chat_id(self.binding.id)
        if not target:
            self._log.warning(
                "feishu webhook binding %s deliver skipped: no chat_id "
                "(config empty AND no inbound seen yet) — set chat_id "
                "in binding config or wait for an inbound message",
                self.binding.id)
            return
        if not _HAS_REQUESTS:
            return
        text = render_event(event)
        if not text:
            return
        token = self._token_cache.get()
        if not token:
            self._log.warning(
                "feishu webhook binding %s deliver skipped: no tenant token "
                "(app_id/app_secret missing or wrong)",
                self.binding.id)
            return
        content = json.dumps({"text": text}, ensure_ascii=False)
        try:
            resp = requests.post(
                f"{self.open_base}/open-apis/im/v1/messages",
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
                timeout=_HTTP_TIMEOUT,
            )
            self._log.info(
                "feishu webhook binding %s deliver to %s: HTTP %s",
                self.binding.id, target, resp.status_code)
        except Exception:
            self._log.exception(
                "feishu webhook binding %s deliver HTTP request failed",
                self.binding.id)


def _factory(binding: ChannelBinding) -> Channel:
    # Dispatch on binding.transport: ws mode requires lark-oapi (handled
    # in feishu_ws.py); webhook mode is the plain HTTP implementation.
    if getattr(binding, "transport", "webhook") == "ws":
        from .feishu_ws import FeishuWsChannel
        return FeishuWsChannel(binding)  # type: ignore[return-value]
    return FeishuChannel(binding)  # type: ignore[return-value]


# Self-register on import so the registry knows about "feishu" bindings.
# Declares both transports: the factory dispatches by binding.transport.
register_channel_kind("feishu", _factory,
                      supported_transports=("ws", "webhook"))


__all__ = ["FeishuChannel"]
