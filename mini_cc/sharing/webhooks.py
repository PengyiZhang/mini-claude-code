"""F7.2 — Webhook event delivery.

Project-scoped webhook subscriptions. Each webhook registers a target
URL plus an optional list of event types to filter on. When the
session's AgentLoop emits an event, the WebhookDispatcher signs the
payload (HMAC-SHA256) and POSTs it asynchronously with a small retry
window.

Persistence: ``<state_root>/<project_id>/webhooks.json``. Keep the
storage shape simple — webhooks are operator-managed, low write rate.

Why a thread pool and not a queue/worker? Delivery is best-effort; the
SSE stream is the authoritative source of truth. If we ever need
at-least-once with backoff we can swap to a durable queue, but for now
fire-and-forget with bounded retries is enough.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover — requests is a hard dep elsewhere
    _HAS_REQUESTS = False


DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds; doubled each retry


def _resolve_host(host: str) -> str | None:
    """Resolve a hostname to its first IPv4/IPv6 string. Returns None on
    DNS failure. Indirected so tests can monkeypatch without touching the
    network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    for info in infos:
        ip = info[4][0]
        # Don't return the port tuple; just the address.
        if ip:
            return ip
    return None


def _is_blocked_ip(ip_str: str) -> bool:
    """True if the address is internal/loopback/private/reserved. These
    ranges are what an SSRF attacker is after (cloud metadata at
    169.254.169.254, internal services on 10.x / 192.168.x, host services
    on 127.0.0.1)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # not a parseable IP — be conservative
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _validate_webhook_url(url: str) -> None:
    """Reject URLs that target internal networks (SSRF defense). Raises
    ValueError on any disallowed target."""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError(f"invalid webhook url: {url!r}")
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"invalid webhook url (no host): {url!r}")
    # Reject well-known internal hostnames by name before DNS — DNS can
    # be made to lie (rebinding) or stubbed in tests, and these names
    # never legitimately appear in a webhook target URL.
    if host.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise ValueError(
            f"webhook host {host!r} is reserved for the local machine")
    # If host is already a literal IP, check directly; otherwise resolve.
    try:
        ipaddress.ip_address(host)
        resolved = host
    except ValueError:
        resolved = _resolve_host(host)
        if resolved is None:
            raise ValueError(
                f"webhook host could not be resolved: {host!r}")
    if _is_blocked_ip(resolved):
        raise ValueError(
            f"webhook target resolves to a non-public address ({resolved}); "
            f"internal/loopback/link-local targets are blocked")


def _webhook_secret() -> str:
    """HMAC secret for signing payloads. Falls back to the share-token
    secret so operators configure one knob for both."""
    for name in ("MINI_CC_WEBHOOK_SECRET", "MINI_CC_SHARE_SECRET"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    # Dev fallback — same caveat as share tokens applies.
    return "dev-webhook-secret"


@dataclass
class Webhook:
    id: str
    url: str
    event_types: list[str] = field(default_factory=list)  # empty → all
    secret: str = ""           # per-hook secret; takes precedence over global
    created_at: str = ""


class WebhookRegistry:
    """Project-scoped registry. Persists to ``webhooks.json`` under the
    project's storage dir. Thread-safe via a single RLock — the write
    rate is operator-driven, not hot."""

    FILENAME = "webhooks.json"

    def __init__(self, state_root: Path, project_id: str):
        self._root = Path(state_root) / project_id
        self._root.mkdir(parents=True, exist_ok=True)
        self._fp = self._root / self.FILENAME
        self._lock = threading.RLock()
        self._hooks: dict[str, Webhook] = self._load()

    def _load(self) -> dict[str, Webhook]:
        if not self._fp.is_file():
            return {}
        try:
            raw = json.loads(self._fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        out: dict[str, Webhook] = {}
        for h in raw if isinstance(raw, list) else []:
            try:
                out[h["id"]] = Webhook(**h)
            except (KeyError, TypeError):
                continue
        return out

    def _persist_locked(self) -> None:
        payload = [{**h.__dict__} for h in self._hooks.values()]
        tmp = self._fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._fp)

    def list(self) -> list[Webhook]:
        with self._lock:
            return list(self._hooks.values())

    def add(self, url: str, event_types: Iterable[str] = ()) -> Webhook:
        _validate_webhook_url(url)
        with self._lock:
            hook = Webhook(
                id=f"wh_{uuid.uuid4().hex[:10]}",
                url=url,
                event_types=[t for t in event_types if t],
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self._hooks[hook.id] = hook
            self._persist_locked()
            return hook

    def remove(self, hook_id: str) -> bool:
        with self._lock:
            if hook_id not in self._hooks:
                return False
            del self._hooks[hook_id]
            self._persist_locked()
            return True

    def matches(self, event_type: str) -> list[Webhook]:
        """Hooks that should receive ``event_type``. Empty event_types
        on a hook means 'subscribe to all'."""
        with self._lock:
            return [h for h in self._hooks.values()
                    if not h.event_types or event_type in h.event_types]


def sign_payload(payload: bytes, secret: str) -> str:
    """HMAC-SHA256 over the raw payload bytes. Sent as ``X-MiniCC-Signature``;
    receivers should recompute and compare in constant time."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class WebhookDispatcher:
    """Fan-out wrapper around an event callback. Calls the original
    ``on_event`` synchronously, then dispatches to matching webhooks
    in a background thread."""

    def __init__(self, registry: WebhookRegistry,
                 project_id: str,
                 session_id: str,
                 inner: Callable[[dict], None] | None = None,
                 deliverer: Callable[[str, bytes, str], None] | None = None):
        self._registry = registry
        self._project_id = project_id
        self._session_id = session_id
        self._inner = inner
        # Injectable for tests; defaults to the threaded _http_deliver.
        self._deliverer = deliverer or self._http_deliver

    def __call__(self, event: dict) -> None:
        if self._inner is not None:
            self._inner(event)
        self._dispatch(event)

    def _dispatch(self, event: dict) -> None:
        etype = str(event.get("type", ""))
        if not etype:
            return
        targets = self._registry.matches(etype)
        if not targets:
            return
        envelope = {
            "project_id": self._project_id,
            "session_id": self._session_id,
            "event": event,
            "ts": int(time.time()),
        }
        body = json.dumps(envelope, ensure_ascii=False,
                          default=str).encode("utf-8")
        for hook in targets:
            secret = hook.secret or _webhook_secret()
            # Each hook gets its own thread so one slow endpoint can't
            # delay another. Bounded by the registry size, which is
            # operator-controlled.
            t = threading.Thread(
                target=self._safe_deliver,
                args=(hook, body, secret),
                daemon=True,
            )
            t.start()

    def _safe_deliver(self, hook: Webhook, body: bytes, secret: str) -> None:
        try:
            self._deliverer(hook.url, body, secret)
        except Exception:
            # Best-effort. Real production should log this.
            pass

    @staticmethod
    def _http_deliver(url: str, body: bytes, secret: str) -> None:
        if not _HAS_REQUESTS:
            return
        sig = sign_payload(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-MiniCC-Signature": sig,
            "X-MiniCC-Event": "any",
        }
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                # allow_redirects=False — the SSRF check ran at registration
                # time, but a 302 from the receiver could redirect to an
                # internal target. Refuse to follow; receiver-side redirects
                # are rare for webhooks and a redirect-to-internal pattern
                # is exactly what we want to block.
                resp = requests.post(url, data=body, headers=headers,
                                     timeout=DEFAULT_TIMEOUT_SECONDS,
                                     allow_redirects=False)
                # 2xx → done. 3xx → treat as misconfigured receiver; don't
                # follow, don't retry forever.
                # 4xx → don't retry, the receiver rejected it.
                if 200 <= resp.status_code < 300 or 300 <= resp.status_code < 500:
                    return
                last_exc = RuntimeError(f"status {resp.status_code}")
            except Exception as e:
                last_exc = e
            time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
        if last_exc:
            raise last_exc
