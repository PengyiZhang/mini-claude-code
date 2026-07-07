"""HTTP-layer smoke test for the channel routes.

Exercises:
- POST /tenants/.../channels (create binding)
- GET /tenants/.../channels (list + secret masking)
- DELETE /tenants/.../channels/{id}
- POST /channels/{id}/webhook (inbound: url_verification handshake + text)
- Unknown-kind rejection at create time
- Tenant boundary on the project-scoped path
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager

# Side-effect: register the feishu channel kind. The TestClient fixture
# below doesn't always trigger lifespan (depends on Starlette version +
# whether `with` syntax is used), so we ensure the kind is registered
# explicitly. Without this, tests fail with "unknown channel kind:
# feishu" when run in isolation.
from mini_cc.channels import _ensure_feishu_loaded
_ensure_feishu_loaded()


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def client(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    c = TestClient(build_app(
        data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
    ))
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    return c


def test_create_list_delete_channel(client):
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu",
                          "config": {"app_id": "a", "app_secret": "secret_val",
                                     "chat_id": "oc_x"},
                          "event_types": ["text", "teammate_message"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["kind"] == "feishu"
    # Secrets must be masked.
    assert out["config"]["app_secret"].endswith("***")
    assert out["config"]["app_id"] == "a"
    cid = out["id"]

    # GET list returns one binding.
    lst = client.get("/tenants/tenant1/projects/p1/channels",
                     headers=AUTH)
    assert lst.status_code == 200
    assert any(b["id"] == cid for b in lst.json())

    # DELETE removes it.
    d = client.delete(f"/tenants/tenant1/projects/p1/channels/{cid}",
                      headers=AUTH)
    assert d.status_code == 204
    lst2 = client.get("/tenants/tenant1/projects/p1/channels",
                      headers=AUTH)
    assert all(b["id"] != cid for b in lst2.json())


def test_unknown_kind_rejected(client):
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "nonexistent",
                          "config": {}})
    assert r.status_code == 400


def test_tenant_boundary_on_channel_route(client):
    # Cross-tenant boundary is enforced by the same helper used for the
    # existing webhooks router (covered by test_round2_hardening). Here
    # we sanity-check the success path: a normal binding gets a chan_ id.
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu",
                          "config": {"app_id": "a",
                                     "verification_token": "vt"}})
    assert r.status_code == 201
    assert r.json()["id"].startswith("chan_")

    # Missing project → 404.
    r2 = client.get("/tenants/tenant1/projects/nope/channels",
                    headers=AUTH)
    assert r2.status_code == 404


def test_inbound_url_verification(client):
    # M2-6: verification material is mandatory — plain mode needs at
    # least a verification_token.
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu", "transport": "webhook",
                          "config": {"app_id": "a",
                                     "verification_token": "vt"}})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # Feishu setup-time handshake.
    body = {"challenge": "abc123", "token": "x", "type": "url_verification"}
    resp = client.post(f"/channels/{cid}/webhook", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


def test_inbound_text_message_returns_200(client, monkeypatch):
    # Create a binding in webhook mode so inbound HTTP webhook parsing
    # applies (default is now ws, where the inbound webhook is a no-op
    # for real messages). verification_token satisfies M2-6.
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu", "transport": "webhook",
                          "config": {"app_id": "a",
                                     "verification_token": "t"}})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # Stub the inbound-turn enqueue so we don't actually fire an LLM.
    from mini_cc.server.routes import channels as chan_routes
    called = {"n": 0, "last": None}

    def _fake_enqueue(sm, project, sid, text, meta):
        called["n"] += 1
        called["last"] = (project.project_id, sid, text, meta)
    monkeypatch.setattr(chan_routes, "_enqueue_inbound_turn", _fake_enqueue)

    payload = {
        "schema": "2.0",
        "header": {"event_id": "e1",
                   "event_type": "im.message.receive_v1",
                   "token": "t"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "m1", "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": "hi from feishu"}),
            },
        },
    }
    resp = client.post(f"/channels/{cid}/webhook", json=payload)
    assert resp.status_code == 200
    assert called["n"] == 1
    pid, sid, text, meta = called["last"]
    assert pid == "p1"
    assert text == "hi from feishu"
    assert meta.get("source") == "feishu"


def test_inbound_unknown_channel_404(client):
    body = {"challenge": "x", "type": "url_verification"}
    resp = client.post("/channels/chan_nonexistent/webhook", json=body)
    assert resp.status_code == 404


# ── Transport field (WS receiver support) ───────────────────────────────

def test_create_ws_binding_returns_transport_ws(client):
    """POST without explicit transport defaults to ws (the recommended
    default). The transport field echoes back on the response so the
    UI can render the right badge without client-side guessing."""
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu",
                          "config": {"app_id": "a", "app_secret": "b"}})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["transport"] == "ws"


def test_create_explicit_webhook_transport(client):
    """Explicit webhook transport round-trips through the API."""
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu", "transport": "webhook",
                          "config": {"app_id": "a"}})
    assert r.status_code == 201
    assert r.json()["transport"] == "webhook"


def test_create_rejects_unsupported_transport(client):
    """transport='ws' on a webhook-only kind fails fast at create time.
    Feishu supports both, so we register a webhook-only kind on the fly."""
    from mini_cc.channels import register_channel_kind
    register_channel_kind("webhook_only_kind",
                          lambda b: None,
                          supported_transports=("webhook",))
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "webhook_only_kind",
                          "transport": "ws", "config": {}})
    assert r.status_code == 400
    assert "ws" in r.json()["error"]["message"]


def test_delete_calls_supervisor_stop(client, monkeypatch):
    """DELETE on a ws binding must stop its receiver before removing
    the record. The route resolves ``get_supervisor`` via ``from
    ...channels import get_supervisor`` so we patch the source module."""
    import mini_cc.channels as channels_mod

    stops = []
    spy = _StopSpy(stops)
    monkeypatch.setattr(channels_mod, "get_supervisor", lambda: spy)
    r = client.post("/tenants/tenant1/projects/p1/channels",
                    headers=AUTH,
                    json={"kind": "feishu", "transport": "ws",
                          "config": {"app_id": "a"}})
    cid = r.json()["id"]
    d = client.delete(f"/tenants/tenant1/projects/p1/channels/{cid}",
                      headers=AUTH)
    assert d.status_code == 204
    assert stops == [("tenant1", "p1", cid)]


class _StopSpy:
    """Minimal supervisor stub: only ``stop_for`` is observed; other
    methods raise so we know they're not called in this path."""

    def __init__(self, stops):
        self.stops = stops

    def stop_for(self, tid, pid, cid):
        self.stops.append((tid, pid, cid))

    def start_for(self, *a, **kw):
        return False  # Don't actually spawn — keep the test WS-clean.
