"""M2-8: per-client-IP rate limiting on the public /shared/* endpoints.

The share token is HMAC-signed but the endpoints are unauthenticated
and brute-forceable by volume; a token-bucket keyed on the client IP
(with env-configurable RPM via MINI_CC_SHARE_RPM) caps the abuse
surface. Denials use the standard 429 envelope + Retry-After header.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.ratelimit import TenantRateLimiter
from mini_cc.session import SessionManager
from mini_cc.sharing.tokens import issue_share_token

AUTH = {"Authorization": "Bearer mck_testkey"}
SECRET = "test-secret-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _share_secret(monkeypatch):
    monkeypatch.setenv("MINI_CC_SHARE_SECRET", SECRET)


def _share_app(tmp_path, *, rpm: int, with_limiter: bool = True):
    import json as _json
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    # Legacy plaintext entry so the fixed test bearer authenticates
    # (same convention as test_p0_files_tree).
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    kwargs = {"share_limiter": TenantRateLimiter(rpm)} if with_limiter else {}
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    **kwargs)
    client = TestClient(app)
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})
    return client


def _token() -> str:
    return issue_share_token("p1", "s1", secret=SECRET)


def test_share_messages_within_limit_ok(tmp_path):
    client = _share_app(tmp_path, rpm=5)
    r = client.get(f"/shared/{_token()}/messages")
    assert r.status_code == 200


def test_share_messages_rate_limited_with_retry_after(tmp_path):
    client = _share_app(tmp_path, rpm=3)
    token = _token()
    statuses = [client.get(f"/shared/{token}/messages").status_code
                for _ in range(6)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]
    r = client.get(f"/shared/{token}/messages")
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    assert r.json()["error"]["code"] == "rate_limited"


def test_share_embed_rate_limited(tmp_path):
    client = _share_app(tmp_path, rpm=2)
    token = _token()
    statuses = [client.get(f"/shared/{token}/embed").status_code
                for _ in range(5)]
    assert statuses[0] == 200
    assert 429 in statuses[2:]


def test_share_rate_limit_env_default(tmp_path, monkeypatch):
    """MINI_CC_SHARE_RPM configures the auto-created limiter when the
    caller doesn't pass one explicitly."""
    monkeypatch.setenv("MINI_CC_SHARE_RPM", "2")
    client = _share_app(tmp_path, rpm=99, with_limiter=False)
    token = _token()
    statuses = [client.get(f"/shared/{token}/embed").status_code
                for _ in range(4)]
    assert statuses[0] == 200
    assert 429 in statuses[2:]
