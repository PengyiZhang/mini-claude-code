"""Asset tokens — signed URLs for handing an attached image to a
vision-capable MCP tool without baking the bytes into the LLM request.

Mirrors the share-token machinery (HMAC-SHA256, ``sh_`` prefix,
base64url payload) but carries an ``a`` (asset_id) claim so the
public asset route can resolve the token back to a stored blob.

Used by the vision-gated image path: when ``MINI_CC_MODEL_VISION=false``
the hydrator swaps each image block for a text block pointing at
``/shared/asset/<token>``. The model then passes that URL to whatever
vision MCP tool is configured.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.sharing.tokens import (  # noqa: E402
    BadShareToken, issue_asset_token, verify_asset_token,
)


SECRET = "test-secret-do-not-use-in-prod"


def test_roundtrip_returns_asset_claims():
    """The defining behavior: mint a token for an asset, verify it,
    get back the same (project, session, asset) triple."""
    t = issue_asset_token("proj_a", "sess_b", "asset_c", secret=SECRET)
    assert t.startswith("sh_")  # same prefix family as share tokens
    claims = verify_asset_token(t, secrets_iter=[SECRET])
    assert claims.project_id == "proj_a"
    assert claims.session_id == "sess_b"
    assert claims.asset_id == "asset_c"
    assert claims.expires_at > claims.issued_at
    assert claims.nonce


def test_asset_token_distinct_from_session_share_token():
    """A share token (no asset claim) must NOT verify as an asset token
    — otherwise anyone with a chat-share link could mint asset URLs."""
    from mini_cc.sharing.tokens import issue_share_token
    share = issue_share_token("p", "s", secret=SECRET)
    try:
        verify_asset_token(share, secrets_iter=[SECRET])
    except BadShareToken:
        return
    raise AssertionError("share token must not validate as asset token")


def test_asset_token_default_ttl_is_short():
    """Asset URLs should expire quickly — only need to outlive a single
    turn. Pin the default to <= 1h so a leaked URL doesn't linger."""
    now = int(time.time())
    t = issue_asset_token("p", "s", "a", secret=SECRET, now=now)
    claims = verify_asset_token(t, secrets_iter=[SECRET], now=now)
    # 1 hour ceiling — anything longer is a footgun for a URL that
    # hands out unauthenticated access to a single asset.
    assert claims.expires_at - claims.issued_at <= 3600


def test_expired_asset_token_rejected():
    """Same expiry discipline as share tokens."""
    now = int(time.time())
    t = issue_asset_token("p", "s", "a", secret=SECRET,
                          ttl_seconds=120, now=now - 1000)
    try:
        verify_asset_token(t, secrets_iter=[SECRET], now=now)
    except BadShareToken as e:
        assert "expired" in str(e).lower()
    else:
        raise AssertionError("expected BadShareToken")


def test_tampered_asset_token_rejected():
    """Flipping a byte in the payload MUST invalidate the signature."""
    t = issue_asset_token("p", "s", "a", secret=SECRET)
    body = t[len("sh_"):]
    payload, sig = body.split(".", 1)
    bad_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    tampered = f"sh_{bad_payload}.{sig}"
    try:
        verify_asset_token(tampered, secrets_iter=[SECRET])
    except BadShareToken:
        return
    raise AssertionError("expected BadShareToken")


def test_wrong_secret_rejected():
    """Signature must validate against one of the configured secrets."""
    t = issue_asset_token("p", "s", "a", secret=SECRET)
    try:
        verify_asset_token(t, secrets_iter=["wrong"])
    except BadShareToken as e:
        assert "signature" in str(e).lower()
    else:
        raise AssertionError("expected BadShareToken")


def test_reissue_yields_distinct_token():
    """Nonce makes re-issuance unique — supports future denylists."""
    a = issue_asset_token("p", "s", "a", secret=SECRET)
    b = issue_asset_token("p", "s", "a", secret=SECRET)
    assert a != b


def test_rotation_accepts_old_secret():
    """Same rotation story as share tokens."""
    old = "old-asset-secret-xxx"
    new = "new-asset-secret-yyy"
    t = issue_asset_token("p", "s", "a", secret=old)
    claims = verify_asset_token(t, secrets_iter=[new, old])
    assert claims.asset_id == "a"


def test_malformed_token_rejected():
    """Garbage inputs raise BadShareToken, not some other exception."""
    for bad in ["", "not_a_token", "sh_", "sh_abc", "sh_abc.def"]:
        try:
            verify_asset_token(bad, secrets_iter=[SECRET])
        except BadShareToken:
            continue
        raise AssertionError(f"expected BadShareToken for {bad!r}")
