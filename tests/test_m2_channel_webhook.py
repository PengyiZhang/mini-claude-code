"""M2-6: channel inbound webhook hardening.

Three layers on the public POST /channels/{id}/webhook surface:
1. Verification material is REQUIRED — a Feishu binding without both
   encrypt_key and verification_token accepts forged payloads that
   trigger paid LLM turns; such inbounds now get 401.
2. Per-channel token-bucket rate limit (MINI_CC_CHANNEL_RPM, default
   30/min) with the standard 429 + Retry-After envelope.
3. Channel-id → project index cached on app.state, maintained by
   create/delete, so inbound lookups stop linear-scanning every
   tenant's projects per request.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.ratelimit import TenantRateLimiter
from mini_cc.session import SessionManager

AUTH = {"Authorization": "Bearer mck_testkey"}


def _client(tmp_path, *, channel_rpm: int | None = 5):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    kwargs = {}
    if channel_rpm is not None:
        kwargs["channel_limiter"] = TenantRateLimiter(channel_rpm)
    c = TestClient(build_app(
        data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm, **kwargs))
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    return c


def _bind(c, config):
    # These tests exercise the public HTTP webhook surface, so the
    # binding must be webhook transport (the create default is ws,
    # whose HTTP path is handshake-only by design).
    r = c.post("/tenants/tenant1/projects/p1/channels",
               headers=AUTH,
               json={"kind": "feishu", "transport": "webhook",
                     "config": config})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _text_payload(token: str = "vt123") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": "e1",
                   "event_type": "im.message.receive_v1",
                   "token": token},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "m1", "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
            },
        },
    }


# ── 1. verification material required ──────────────────────────────

def test_inbound_rejected_without_any_verification_material(tmp_path, monkeypatch):
    c = _client(tmp_path)
    cid = _bind(c, {"app_id": "a", "app_secret": "s"})  # no enc/vt
    from mini_cc.server.routes import channels as chan_routes
    fired = {"n": 0}
    monkeypatch.setattr(chan_routes, "_enqueue_inbound_turn",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    r = c.post(f"/channels/{cid}/webhook", json=_text_payload())
    assert r.status_code == 401
    assert "verification" in r.json()["error"]["message"].lower()
    assert fired["n"] == 0


def test_inbound_accepted_with_verification_token(tmp_path, monkeypatch):
    c = _client(tmp_path)
    cid = _bind(c, {"app_id": "a", "app_secret": "s",
                    "verification_token": "vt123"})
    from mini_cc.server.routes import channels as chan_routes
    fired = {"n": 0}
    monkeypatch.setattr(chan_routes, "_enqueue_inbound_turn",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    r = c.post(f"/channels/{cid}/webhook", json=_text_payload("vt123"))
    assert r.status_code == 200
    assert fired["n"] == 1


def test_inbound_wrong_token_silently_dropped(tmp_path, monkeypatch):
    c = _client(tmp_path)
    cid = _bind(c, {"app_id": "a", "verification_token": "vt123"})
    from mini_cc.server.routes import channels as chan_routes
    monkeypatch.setattr(chan_routes, "_enqueue_inbound_turn",
                        lambda *a, **k: pytest.fail("must not fire"))
    r = c.post(f"/channels/{cid}/webhook", json=_text_payload("WRONG"))
    assert r.status_code == 200  # Feishu expects 200 on ignored events


# ── 2. per-channel rate limit ──────────────────────────────────────

def test_inbound_rate_limited(tmp_path):
    c = _client(tmp_path, channel_rpm=3)
    cid = _bind(c, {"app_id": "a", "verification_token": "vt"})
    body = {"challenge": "x", "type": "url_verification"}
    statuses = [c.post(f"/channels/{cid}/webhook", json=body).status_code
                for _ in range(6)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]
    r = c.post(f"/channels/{cid}/webhook", json=body)
    assert r.status_code == 429
    assert r.headers.get("Retry-After")
    assert r.json()["error"]["code"] == "rate_limited"


# ── 3. binding index ───────────────────────────────────────────────

def test_channel_index_populated_and_invalidated(tmp_path):
    c = _client(tmp_path)
    cid = _bind(c, {"app_id": "a", "verification_token": "vt"})
    index = c.app.state.channel_index
    assert index.get(cid) == ("tenant1", "p1")
    r = c.delete(f"/tenants/tenant1/projects/p1/channels/{cid}",
                 headers=AUTH)
    assert r.status_code == 204
    assert cid not in index
    r = c.post(f"/channels/{cid}/webhook",
               json={"challenge": "x", "type": "url_verification"})
    assert r.status_code == 404
