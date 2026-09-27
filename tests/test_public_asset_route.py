"""Public /shared/asset/{token} route.

When the model is configured as vision-incapable, image attachments are
handed to MCP vision tools via a signed URL. The MCP tool (running as
a subprocess) needs to fetch the bytes without an API key — the token
IS the auth.

These tests pin the contract:
- valid token → bytes 200, correct Content-Type
- bad signature → 401
- valid token but asset deleted → 404
- no API key required on this route
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.sharing.tokens import issue_asset_token


AUTH = {"Authorization": "Bearer mck_testkey"}

PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
    "4e44ae426082"
)


@pytest.fixture
def env_secret(monkeypatch):
    """Pin a known HMAC secret so tokens verify in-test."""
    monkeypatch.setenv("MINI_CC_SHARE_SECRET", "route-test-secret")
    return "route-test-secret"


@pytest.fixture
def client(tmp_path, env_secret):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    pm.create("tenant1", "p1")
    sm = SessionManager(pm)
    c = TestClient(build_app(data_dir=tmp_path, key_registry=reg,
                             pm=pm, sm=sm))
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    c.post("/tenants/tenant1/projects/p1/sessions",
           headers=AUTH, json={"session_id": "sess-1"})
    return c


def _upload(client) -> str:
    """Upload a PNG via the authenticated route; return its asset_id."""
    png = bytes.fromhex(PNG_HEX)
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/sess-1/assets",
        headers=AUTH,
        files={"file": ("a.png", png, "image/png")},
    )
    assert r.status_code == 201, r.text
    return r.json()["asset_id"]


def test_valid_token_returns_bytes_no_auth(client, env_secret):
    """The defining behavior: a valid signed URL returns the asset
    bytes with NO Authorization header."""
    aid = _upload(client)
    token = issue_asset_token("p1", "sess-1", aid, secret=env_secret)
    r = client.get(f"/shared/asset/{token}")
    assert r.status_code == 200, r.text
    assert r.content == bytes.fromhex(PNG_HEX)
    assert r.headers["content-type"] == "image/png"


def test_bad_signature_rejected(client, env_secret):
    """A token signed with a different secret must 401."""
    aid = _upload(client)
    token = issue_asset_token("p1", "sess-1", aid, secret="wrong-secret")
    r = client.get(f"/shared/asset/{token}")
    assert r.status_code == 401


def test_expired_token_rejected(client, env_secret):
    """Past-expiry token must 401."""
    import time
    aid = _upload(client)
    token = issue_asset_token("p1", "sess-1", aid, secret=env_secret,
                              ttl_seconds=120, now=int(time.time()) - 1000)
    r = client.get(f"/shared/asset/{token}")
    assert r.status_code == 401


def test_valid_token_missing_asset_404s(client, env_secret):
    """Signature is fine, but the asset_id refers to a deleted / never
    existed blob. 404, not 500."""
    token = issue_asset_token("p1", "sess-1", "ghost-asset",
                              secret=env_secret)
    r = client.get(f"/shared/asset/{token}")
    assert r.status_code == 404


def test_share_token_does_not_work_for_asset_route(client, env_secret):
    """A session-share token must NOT authenticate the asset route —
    prevents anyone holding a chat-share link from minting arbitrary
    asset URLs."""
    from mini_cc.sharing.tokens import issue_share_token
    share = issue_share_token("p1", "sess-1", secret=env_secret)
    r = client.get(f"/shared/asset/{share}")
    assert r.status_code == 401


def test_malformed_token_rejected(client):
    """Pure garbage on the path → 401 (not 500)."""
    r = client.get("/shared/asset/not-a-real-token")
    assert r.status_code == 401
