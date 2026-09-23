"""Phase D — HTTP integration: scope enforcement, expiry, migration."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


@pytest.fixture
def scope_app(tmp_path):
    """App with three keys for tenant1: full (star), read-only, sessions-writer."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    full = reg.generate("tenant1", scopes=["*"], label="full")
    reader = reg.generate("tenant1", scopes=["read:*"], label="ci")
    writer = reg.generate("tenant1",
                          scopes=["sessions:write", "projects:write"],
                          label="runner")
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    yield TestClient(app), {
        "full": full.key,
        "reader": reader.key,
        "writer": writer.key,
    }


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── scope enforcement ────────────────────────────────────────────────────

def test_full_scope_can_create_and_list_projects(scope_app):
    client, keys = scope_app
    r = client.post("/tenants/tenant1/projects",
                    headers=auth(keys["full"]),
                    json={"project_id": "p1"})
    assert r.status_code == 201
    r = client.get("/tenants/tenant1/projects",
                   headers=auth(keys["full"]))
    assert r.status_code == 200


def test_read_only_scope_can_list_but_not_create(scope_app):
    client, keys = scope_app
    # POST (write) → 403
    r = client.post("/tenants/tenant1/projects",
                    headers=auth(keys["reader"]),
                    json={"project_id": "p1"})
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["details"]["code"] == "insufficient_scope"
    assert body["error"]["details"]["required"] == "projects:write"
    assert body["error"]["details"]["held"] == ["read:*"]
    # WWW-Authenticate header carries the scope hint
    assert r.headers.get("WWW-Authenticate") == 'Bearer scope="projects:write"'
    # GET (read) → 200
    r = client.get("/tenants/tenant1/projects",
                   headers=auth(keys["reader"]))
    assert r.status_code == 200


def test_partial_writer_can_create_sessions_but_not_list(scope_app):
    """Writer holds sessions:write + projects:write but no :read anywhere."""
    client, keys = scope_app
    # Create the project first (writer can — has projects:write)
    r = client.post("/tenants/tenant1/projects",
                    headers=auth(keys["writer"]),
                    json={"project_id": "p1"})
    assert r.status_code == 201
    # Create a session — has sessions:write → 201
    r = client.post("/tenants/tenant1/projects/p1/sessions",
                    headers=auth(keys["writer"]),
                    json={"session_id": "s1"})
    assert r.status_code == 201
    # List sessions — needs sessions:read → 403
    r = client.get("/tenants/tenant1/projects/p1/sessions",
                   headers=auth(keys["writer"]))
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required"] == "sessions:read"


def test_files_scope_enforced_on_resources_routes(scope_app):
    client, keys = scope_app
    # Create project with full key
    client.post("/tenants/tenant1/projects",
                headers=auth(keys["full"]),
                json={"project_id": "p1"})
    # Reader can GET /files/tree but not POST /files/mkdir
    r = client.get("/tenants/tenant1/projects/p1/files/tree",
                   headers=auth(keys["reader"]))
    assert r.status_code == 200
    r = client.post("/tenants/tenant1/projects/p1/files/mkdir",
                    headers=auth(keys["reader"]),
                    params={"path": "newdir"})
    assert r.status_code == 403
    assert r.json()["error"]["details"]["required"] == "files:write"


def test_insufficient_scope_does_not_leak_tenant_existence(scope_app):
    """A reader hitting a write endpoint on a tenant they don't own
    gets 401/404 (tenant mismatch) BEFORE the scope check — but if
    tenant matches, scope check fires with 403 (no info leak on
    other tenants' data)."""
    client, keys = scope_app
    # tenant2 has no project — but writer is bound to tenant1
    r = client.post("/tenants/tenant2/projects",
                    headers=auth(keys["writer"]),
                    json={"project_id": "x"})
    # Tenant mismatch → 403 forbidden, NOT insufficient_scope
    assert r.status_code == 403
    assert r.json()["error"]["details"] == {} or \
           r.json()["error"]["details"].get("code") != "insufficient_scope"


# ── expiry ───────────────────────────────────────────────────────────────

def test_expired_key_returns_401(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1", expires_in="1s")
    # Force expiry on disk
    raw = reg._read_raw()
    raw[reg._find_name(raw, rec.key)]["expires_at"] = "2000-01-01T00:00:00Z"
    reg._write(raw)
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)
    r = client.get("/tenants/tenant1/projects",
                   headers=auth(rec.key))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# ── migration ────────────────────────────────────────────────────────────

def test_migrated_bare_string_key_still_works_as_star(tmp_path):
    """Pre-Phase-D keys.json (bare str values) should still authenticate."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"mck_legacy": "tenant1"}))
    reg = TenantKeyRegistry(path)
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)
    # Migrated key has scopes=["*"] → can do anything
    r = client.post("/tenants/tenant1/projects",
                    headers=auth("mck_legacy"),
                    json={"project_id": "p1"})
    assert r.status_code == 201
    r = client.get("/tenants/tenant1/projects",
                   headers=auth("mck_legacy"))
    assert r.status_code == 200


# ── rate-limit + scope coexistence ───────────────────────────────────────

def test_scope_check_runs_before_rate_limit(tmp_path):
    """A scope-rejected request must NOT burn a rate-limit token (scope
    is the inner dep, rate-limit chains off it)."""
    from mini_cc.server.ratelimit import TenantRateLimiter
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reader = reg.generate("tenant1", scopes=["read:*"])
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    limiter = TenantRateLimiter(default_rpm=1)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    rate_limiter=limiter)
    client = TestClient(app)
    # First request: scope-rejected POST (no token burned because scope dep fails first)
    r = client.post("/tenants/tenant1/projects",
                    headers=auth(reader.key),
                    json={"project_id": "p1"})
    assert r.status_code == 403
    # The lone token is still available for a legitimate read
    r = client.get("/tenants/tenant1/projects",
                   headers=auth(reader.key))
    assert r.status_code == 200
