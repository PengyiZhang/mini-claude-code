"""F7.1 — signed share tokens.

A share token is a URL-safe string that grants read-only access to a
specific session without requiring an API key. The token carries its
claims in plaintext (base64url-encoded JSON) plus an HMAC-SHA256
signature over those claims using a server-side secret.

Why not JWT? We don't need the header/jwk/alg-negotiation surface; a
fixed HMAC with a single secret keeps the verifier trivially safe
against alg=none confusion.

Secret rotation: read from ``MINI_CC_SHARE_SECRET`` (or
``MINI_CC_SHARE_SECRETS`` as a comma-separated list to support
graceful rotation). When neither is set, a derived secret is used so
tokens work in dev — but production deployments MUST set an explicit
secret to be considered safe.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

# Default TTL: 7 days. Long enough to be useful for sharing a chat,
# short enough that stale links don't linger indefinitely.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_PREFIX = "sh_"  # so we can recognise our own tokens at a glance


@dataclass
class ShareClaims:
    project_id: str
    session_id: str
    mode: str            # "read" (only supported mode for now)
    issued_at: int       # unix seconds
    expires_at: int      # unix seconds
    nonce: str           # uniqueness to make re-issuance distinct


class BadShareToken(Exception):
    """Raised when a token is malformed, tampered with, or expired."""


def _active_secrets() -> list[str]:
    """Return the list of acceptable HMAC secrets, in priority order.

    Reads ``MINI_CC_SHARE_SECRETS`` (comma-separated, supports rotation)
    first, falling back to ``MINI_CC_SHARE_SECRET`` (single). When
    neither is set we derive one from the process — sufficient for local
    dev but will invalidate all tokens on restart, which is the desired
    "don't ship without configuring" forcing function.
    """
    raw_list = os.environ.get("MINI_CC_SHARE_SECRETS")
    if raw_list and raw_list.strip():
        parts = [s.strip() for s in raw_list.split(",") if s.strip()]
        if parts:
            return parts
    single = os.environ.get("MINI_CC_SHARE_SECRET")
    if single and single.strip():
        return [single.strip()]
    # Dev fallback — derived value so the same process can issue + verify.
    return [_dev_fallback_secret()]


def _dev_fallback_secret() -> str:
    """Stable per-process secret derived from a module-level random
    value. NOT safe for production — only used when no env secret is
    configured. Exposed via :func:`warn_if_default_secret` so operators
    get a clear signal."""
    global _DEV_SECRET
    if _DEV_SECRET is None:
        _DEV_SECRET = "dev-fallback:" + secrets.token_hex(32)
    return _DEV_SECRET


_DEV_SECRET: str | None = None


def warn_if_default_secret() -> bool:
    """True when we're using the dev fallback (i.e. no env secret set).
    Operators should surface this in startup logs."""
    if os.environ.get("MINI_CC_SHARE_SECRETS", "").strip():
        return False
    if os.environ.get("MINI_CC_SHARE_SECRET", "").strip():
        return False
    return True


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _ub64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"),
                   payload_b64.encode("ascii"),
                   hashlib.sha256).digest()
    return _b64(mac)


def issue_share_token(project_id: str,
                      session_id: str,
                      mode: str = "read",
                      ttl_seconds: int = DEFAULT_TTL_SECONDS,
                      secret: str | None = None,
                      now: int | None = None) -> str:
    """Mint a signed share token for the given (project, session).

    A fresh nonce makes each issuance unique so re-sharing the same
    session yields a different token (and the previous one can be
    independently revoked via denylist, if you build one).
    """
    if mode != "read":
        raise ValueError(f"unsupported share mode: {mode!r}")
    if not project_id or not session_id:
        raise ValueError("project_id and session_id are required")
    sec = secret or _active_secrets()[0]
    now_ts = int(now if now is not None else time.time())
    claims = {
        "p": project_id,
        "s": session_id,
        "m": mode,
        "iat": now_ts,
        "exp": now_ts + max(60, int(ttl_seconds)),
        "n": secrets.token_hex(6),
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    sig = _sign(payload, sec)
    return f"{_PREFIX}{payload}.{sig}"


def verify_share_token(token: str,
                       secrets_iter: Iterable[str] | None = None,
                       now: int | None = None) -> ShareClaims:
    """Verify signature + expiry. Raises :class:`BadShareToken` on any
    failure; returns parsed claims on success."""
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise BadShareToken("missing or malformed token prefix")
    body = token[len(_PREFIX):]
    if "." not in body:
        raise BadShareToken("missing signature separator")
    payload_b64, sig = body.rsplit(".", 1)
    candidate_secrets = list(secrets_iter) if secrets_iter is not None else _active_secrets()
    # Constant-time compare across each candidate secret (rotation).
    expected_sigs = [_sign(payload_b64, s) for s in candidate_secrets]
    if not any(hmac.compare_digest(sig, exp) for exp in expected_sigs):
        raise BadShareToken("signature mismatch")
    try:
        raw = _ub64(payload_b64)
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        raise BadShareToken(f"malformed payload: {e}") from e
    if not isinstance(claims, dict):
        raise BadShareToken("payload is not an object")
    try:
        p = str(claims["p"])
        s = str(claims["s"])
        m = str(claims["m"])
        iat = int(claims["iat"])
        exp = int(claims["exp"])
        nonce = str(claims.get("n", ""))
    except (KeyError, TypeError, ValueError) as e:
        raise BadShareToken(f"missing or invalid claim: {e}") from e
    if m != "read":
        raise BadShareToken(f"unsupported mode in token: {m!r}")
    now_ts = int(now if now is not None else time.time())
    if now_ts >= exp:
        raise BadShareToken("token expired")
    if iat > now_ts + 60:
        # Token issued in the future — clock skew tolerance of 60s.
        raise BadShareToken("token issued in the future")
    return ShareClaims(project_id=p, session_id=s, mode=m,
                       issued_at=iat, expires_at=exp, nonce=nonce)
