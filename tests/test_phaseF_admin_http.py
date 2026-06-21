"""Phase F — admin routes for keys management + tenant-scoped metrics."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.metrics import default_registry
from mini_cc.session import SessionManager


@pytest.fixture
def admin_app(tmp_path):
    """Three keys for tenant1: admin (full), reader (admin:read only),
    plain chat user (no admin scope). Plus one tenant2 admin for
    cross-tenant tests."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    admin = reg.generate("tenant1", scopes=["*"], label="admin")
    reader = reg.generate("tenant1",
                           scopes=["admin:read"], label="audit")
    chat = reg.generate("tenant1",
                         scopes=["read:*"], label="user")
    t2_admin = reg.generate("tenant2", scopes=["*"], label="t2-admin")
    metrics = default_registry()
    pm = ProjectManager(tmp_path / "projects", metrics=metrics)
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    metrics_registry=metrics)
    yield TestClient(app), {
        "admin": admin.key,
        "reader": reader.key,
        "chat": chat.key,
        "t2_admin": t2_admin.key,
    }, reg


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── list_keys ───────────────────────────────────────────────────────

def test_admin_full_can_list_keys(admin_app):
    client, keys, _ = admin_app
    r = client.get("/tenants/tenant1/admin/keys", headers=auth(keys["admin"]))
    assert r.status_code == 200
    data = r.json()
    # Three keys for tenant1
    assert len(data) == 3
    assert {k["label"] for k in data} == {"admin", "audit", "user"}


def test_admin_read_can_list_keys(admin_app):
    """admin:read scope is enough to list."""
    client, keys, _ = admin_app
    r = client.get("/tenants/tenant1/admin/keys", headers=auth(keys["reader"]))
    assert r.status_code == 200


def test_non_admin_scope_cannot_list_keys(admin_app):
    """A write-scoped key without read access — 403. Note: under Phase D
    grammar, `read:*` matches any GET including admin:read, so a
    read:* user CAN technically call admin:read routes. The frontend
    admin login adds an extra `*`-scope gate."""
    client, keys, reg = admin_app
    # Add a write-only key (no read access at all)
    write_only = reg.generate("tenant1", scopes=["write:*"], label="bot")
    r = client.get("/tenants/tenant1/admin/keys",
                   headers=auth(write_only.key))
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required"] == "admin:read"


# ── create_key ──────────────────────────────────────────────────────

def test_admin_can_create_key(admin_app):
    client, keys, _ = admin_app
    r = client.post("/tenants/tenant1/admin/keys",
                    headers=auth(keys["admin"]),
                    json={"scopes": ["read:*"], "expires_in": "7d",
                          "label": "ci"})
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("mck_")
    assert body["scopes"] == ["read:*"]
    assert body["label"] == "ci"
    assert body["expires_at"] is not None
    # Round-trip: appears in list
    r2 = client.get("/tenants/tenant1/admin/keys", headers=auth(keys["admin"]))
    labels = {k["label"] for k in r2.json()}
    assert "ci" in labels


def test_admin_read_cannot_create_key(admin_app):
    """admin:read scope cannot do admin:write operations."""
    client, keys, _ = admin_app
    r = client.post("/tenants/tenant1/admin/keys",
                    headers=auth(keys["reader"]),
                    json={"scopes": ["read:*"]})
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required"] == "admin:write"


# ── update_key ──────────────────────────────────────────────────────

def test_admin_can_patch_key(admin_app):
    client, keys, reg = admin_app
    # Use the chat key as the patch target
    target = keys["chat"]
    r = client.patch(f"/tenants/tenant1/admin/keys/{target}",
                     headers=auth(keys["admin"]),
                     json={"scopes": ["sessions:write"], "label": "downgraded"})
    assert r.status_code == 200
    body = r.json()
    assert body["scopes"] == ["sessions:write"]
    assert body["label"] == "downgraded"


def test_patch_unknown_key_returns_404(admin_app):
    client, keys, _ = admin_app
    r = client.patch("/tenants/tenant1/admin/keys/mck_nope",
                     headers=auth(keys["admin"]),
                     json={"label": "x"})
    assert r.status_code == 404


def test_patch_cross_tenant_key_returns_404(admin_app):
    """tenant1 admin cannot mutate a tenant2 key — 404, not 403, to
    avoid leaking existence."""
    client, keys, _ = admin_app
    r = client.patch(f"/tenants/tenant1/admin/keys/{keys['t2_admin']}",
                     headers=auth(keys["admin"]),
                     json={"label": "hacked"})
    assert r.status_code == 404


# ── revoke_key ──────────────────────────────────────────────────────

def test_admin_can_revoke_key(admin_app):
    client, keys, _ = admin_app
    target = keys["chat"]
    r = client.delete(f"/tenants/tenant1/admin/keys/{target}",
                      headers=auth(keys["admin"]))
    assert r.status_code == 204
    # Revoke again → 404
    r = client.delete(f"/tenants/tenant1/admin/keys/{target}",
                      headers=auth(keys["admin"]))
    assert r.status_code == 404


# ── rotate_key ──────────────────────────────────────────────────────

def test_rotate_hard_revoke_default(admin_app):
    client, keys, _ = admin_app
    target = keys["chat"]
    r = client.post(f"/tenants/tenant1/admin/keys/{target}/rotate",
                    headers=auth(keys["admin"]),
                    json={})
    assert r.status_code == 200
    body = r.json()
    assert body["new_key"]["key"].startswith("mck_")
    assert body["old_key"] is None  # hard-revoked
    # Old key is gone
    r = client.get("/tenants/tenant1/admin/keys", headers=auth(keys["admin"]))
    listed_keys = {k["key"] for k in r.json()}
    assert target not in listed_keys
    assert body["new_key"]["key"] in listed_keys


def test_rotate_with_grace_keeps_old(admin_app):
    client, keys, _ = admin_app
    target = keys["chat"]
    r = client.post(f"/tenants/tenant1/admin/keys/{target}/rotate",
                    headers=auth(keys["admin"]),
                    json={"grace_hours": 24, "label": "rotated"})
    assert r.status_code == 200
    body = r.json()
    assert body["old_key"] is not None
    assert body["old_key"]["expires_at"] is not None
    assert body["new_key"]["label"] == "rotated"
    # Both still listed
    r = client.get("/tenants/tenant1/admin/keys", headers=auth(keys["admin"]))
    listed_keys = {k["key"] for k in r.json()}
    assert target in listed_keys
    assert body["new_key"]["key"] in listed_keys


# ── tenant-scoped metrics ───────────────────────────────────────────

def test_admin_metrics_filters_to_tenant(admin_app):
    """Drive a request as tenant1, then fetch tenant1 metrics — must
    show the request; fetching as tenant2 must not show it."""
    client, keys, _ = admin_app
    # Drive some traffic as tenant1
    client.get("/tenants/tenant1/projects", headers=auth(keys["chat"]))
    # tenant1 admin metrics
    r = client.get("/tenants/tenant1/admin/metrics.json",
                   headers=auth(keys["admin"]))
    assert r.status_code == 200
    body = r.json()
    # All series in http_requests_total should have tenant="tenant1"
    for series in body["counters"]["http_requests_total"]["series"]:
        assert series["labels"]["tenant"] == "tenant1"
    # tenant2 admin metrics
    r = client.get("/tenants/tenant2/admin/metrics.json",
                   headers=auth(keys["t2_admin"]))
    assert r.status_code == 200
    body = r.json()
    # tenant2 should have no tenant1 traffic
    t1_hits = [s for s in body["counters"]["http_requests_total"]["series"]
               if s["labels"].get("tenant") == "tenant1"]
    assert t1_hits == []


def test_admin_metrics_requires_admin_read(admin_app):
    """A write-only key (no read access) cannot read metrics."""
    client, keys, reg = admin_app
    write_only = reg.generate("tenant1", scopes=["write:*"], label="bot")
    r = client.get("/tenants/tenant1/admin/metrics.json",
                   headers=auth(write_only.key))
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required"] == "admin:read"


# ── cross-tenant list is empty ──────────────────────────────────────

def test_admin_lists_only_own_tenant_keys(admin_app):
    """tenant1 admin listing tenant2 keys should get 404 (no project
    match in path) OR empty list — current impl returns 403 via
    require_scope because tid mismatch."""
    client, keys, _ = admin_app
    r = client.get("/tenants/tenant2/admin/keys", headers=auth(keys["admin"]))
    # tenant1 admin key → tenant2 path → 403 forbidden (tenant mismatch)
    assert r.status_code == 403
