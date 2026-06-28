"""F7.1 — share-token sign/verify roundtrip + tamper/expiry checks."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.sharing.tokens import (  # noqa: E402
    BadShareToken, issue_share_token, verify_share_token,
)


SECRET = "test-secret-do-not-use-in-prod"


def test_roundtrip_returns_claims():
    t = issue_share_token("proj_a", "sess_b", secret=SECRET)
    assert t.startswith("sh_")
    claims = verify_share_token(t, secrets_iter=[SECRET])
    assert claims.project_id == "proj_a"
    assert claims.session_id == "sess_b"
    assert claims.mode == "read"
    assert claims.expires_at > claims.issued_at
    assert claims.nonce


def test_default_secret_warning_true_without_env(monkeypatch):
    monkeypatch.delenv("MINI_CC_SHARE_SECRET", raising=False)
    monkeypatch.delenv("MINI_CC_SHARE_SECRETS", raising=False)
    from mini_cc.sharing.tokens import warn_if_default_secret
    assert warn_if_default_secret() is True


def test_default_secret_warning_false_with_env(monkeypatch):
    monkeypatch.setenv("MINI_CC_SHARE_SECRET", "x")
    from mini_cc.sharing.tokens import warn_if_default_secret
    assert warn_if_default_secret() is False


def test_token_with_wrong_secret_rejected():
    t = issue_share_token("p", "s", secret=SECRET)
    try:
        verify_share_token(t, secrets_iter=["wrong"])
    except BadShareToken as e:
        assert "signature" in str(e).lower()
    else:
        raise AssertionError("expected BadShareToken")


def test_tampered_payload_rejected():
    t = issue_share_token("p", "s", secret=SECRET)
    # Flip a character in the payload portion (before the dot).
    body = t[len("sh_"):]
    payload, sig = body.split(".", 1)
    bad_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    tampered = f"sh_{bad_payload}.{sig}"
    try:
        verify_share_token(tampered, secrets_iter=[SECRET])
    except BadShareToken:
        return
    raise AssertionError("expected BadShareToken")


def test_expired_token_rejected():
    now = int(time.time())
    t = issue_share_token("p", "s", secret=SECRET,
                          ttl_seconds=120, now=now - 1000)
    try:
        verify_share_token(t, secrets_iter=[SECRET], now=now)
    except BadShareToken as e:
        assert "expired" in str(e).lower()
    else:
        raise AssertionError("expected BadShareToken")


def test_rotation_accepts_old_secret():
    old = "old-secret-xxx"
    new = "new-secret-yyy"
    # Token signed with old secret should still verify when both are
    # passed as candidate secrets (graceful rotation).
    t = issue_share_token("p", "s", secret=old)
    claims = verify_share_token(t, secrets_iter=[new, old])
    assert claims.project_id == "p"


def test_reissue_yields_distinct_token():
    a = issue_share_token("p", "s", secret=SECRET)
    b = issue_share_token("p", "s", secret=SECRET)
    assert a != b  # nonce makes them differ


def test_malformed_token_rejected():
    for bad in ["", "not_a_token", "sh_", "sh_abc", "sh_abc.def"]:
        try:
            verify_share_token(bad, secrets_iter=[SECRET])
        except BadShareToken:
            continue
        raise AssertionError(f"expected BadShareToken for {bad!r}")
