"""Phase B: HTTP-level session resume behaviors."""
from __future__ import annotations

import json
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.config import set_default_config
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


# ── Mock Anthropic client ──────────────────────────────────────────────────

class _Stream:
    def __init__(self, content):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter([])

    def get_final_message(self):
        return _Msg(self._content)

    def close(self):
        pass


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.stop_reason = "end_turn"


class _M:
    def __init__(self, content): self._c = content
    @property
    def messages(self): return self
    def stream(self, **kw): return _Stream(self._c)


class _MockConfig:
    def __init__(self, client): self._client = client
    def build_client(self): return self._client
    @property
    def primary_model(self): return "mock-model"
    @property
    def fallback_model(self): return None


@pytest.fixture
def app(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)

    fake = _M([_Block(type="text", text="hello")])
    set_default_config(_MockConfig(fake))  # type: ignore[arg-type]

    yield TestClient(app), pm, sm, tmp_path

    import mini_cc.config as cfg
    cfg._DEFAULT = None


def _setup_project(client):
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})


# ── POST /sessions idempotent resume ───────────────────────────────────────

def test_post_sessions_returns_201_then_200_for_same_id(app):
    client, *_ = app
    _setup_project(client)

    r1 = client.post("/tenants/tenant1/projects/p1/sessions",
                     headers=AUTH, json={"session_id": "s1"})
    assert r1.status_code == 201
    assert r1.json()["created"] is True

    r2 = client.post("/tenants/tenant1/projects/p1/sessions",
                     headers=AUTH, json={"session_id": "s1"})
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["session_id"] == "s1"


# ── GET /sessions returns SessionMeta ──────────────────────────────────────

def test_get_sessions_returns_session_meta_with_in_memory(app):
    client, *_ = app
    _setup_project(client)
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "warm"})

    r = client.get("/tenants/tenant1/projects/p1/sessions", headers=AUTH)
    assert r.status_code == 200
    metas = {m["session_id"]: m for m in r.json()}
    assert "warm" in metas
    assert metas["warm"]["in_memory"] is True
    assert metas["warm"]["message_count"] == 0
    assert metas["warm"]["created_at"]
    assert metas["warm"]["last_active_at"]


# ── POST /sessions/{sid}/resume ────────────────────────────────────────────

def test_resume_warms_cold_session(app):
    client, pm, sm, tmp_path = app
    _setup_project(client)
    # Seed a cold session directly via storage.
    project = pm.get("p1")
    project.storage.save_messages("p1", "cold",
                                  [{"role": "user", "content": "hi"}])

    r = client.post("/tenants/tenant1/projects/p1/sessions/cold/resume",
                    headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "cold"
    assert body["in_memory"] is True


def test_resume_unknown_session_returns_404(app):
    client, *_ = app
    _setup_project(client)
    r = client.post("/tenants/tenant1/projects/p1/sessions/ghost/resume",
                    headers=AUTH)
    assert r.status_code == 404


def test_resume_idempotent_when_already_warm(app):
    client, *_ = app
    _setup_project(client)
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s_warm"})
    r1 = client.post("/tenants/tenant1/projects/p1/sessions/s_warm/resume",
                     headers=AUTH)
    r2 = client.post("/tenants/tenant1/projects/p1/sessions/s_warm/resume",
                     headers=AUTH)
    assert r1.status_code == 200
    assert r2.status_code == 200


# ── Auto-resume on send ────────────────────────────────────────────────────

def test_send_auto_resumes_cold_session(app):
    """POST /sessions/{sid}/send on a session that exists on disk but not
    in memory auto-warms it instead of 404ing."""
    client, pm, sm, tmp_path = app
    _setup_project(client)
    project = pm.get("p1")
    project.storage.save_messages("p1", "cold_send",
                                  [{"role": "user", "content": "hi"}])
    # Sanity: not in memory
    assert all(m.session_id != "cold_send" for m in sm.list("p1")
               if m.in_memory)

    r = client.post("/tenants/tenant1/projects/p1/sessions/cold_send/send",
                    headers=AUTH, json={"user_input": "again"})
    assert r.status_code == 200
    # Drain the SSE stream so the worker thread exits.
    for _ in r.iter_lines():
        pass
    # Now warm.
    metas = {m.session_id: m for m in sm.list("p1")}
    assert metas["cold_send"].in_memory is True


# ── Restart simulation: fresh SessionManager sees old session ─────────────

def test_restart_simulation_lists_old_sessions(app):
    client, pm, sm, tmp_path = app
    _setup_project(client)
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "persist"})

    # "Restart": build fresh managers from same data dir, new app.
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    pm2 = ProjectManager(tmp_path / "projects")
    sm2 = SessionManager(pm2)
    app2 = build_app(data_dir=tmp_path, key_registry=reg, pm=pm2, sm=sm2)
    fake = _M([_Block(type="text", text="hello")])
    set_default_config(_MockConfig(fake))  # type: ignore[arg-type]
    client2 = TestClient(app2)

    # Old session visible from list
    r = client2.get("/tenants/tenant1/projects/p1/sessions", headers=AUTH)
    metas = {m["session_id"]: m for m in r.json()}
    assert "persist" in metas
    assert metas["persist"]["in_memory"] is False

    # Resume works
    r2 = client2.post("/tenants/tenant1/projects/p1/sessions/persist/resume",
                      headers=AUTH)
    assert r2.status_code == 200

    import mini_cc.config as cfg
    cfg._DEFAULT = None
