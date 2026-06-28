"""W6 — drive endpoint smoke test (full UI tests live under e2e/)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def client(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    import json as _json
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    return TestClient(app)


def _full_setup(client):
    """Create tenant, project, definition, and run."""
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})


def test_drive_unknown_session_returns_404(client):
    _full_setup(client)
    # Create a definition + run via HTTP.
    r = client.post("/tenants/tenant1/projects/p1/workflow-definitions",
                    headers=AUTH,
                    json={"name": "wf",
                          "steps": [{"id": "s1", "prompt": "go"}]})
    def_id = r.json()["def_id"]
    r2 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-definitions/{def_id}/runs",
        headers=AUTH, json={})
    run_id = r2.json()["run_id"]
    # Drive with a non-existent session.
    r3 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/drive",
        headers=AUTH, json={"session_id": "ghost"})
    assert r3.status_code == 404


def test_drive_unknown_run_returns_404(client):
    _full_setup(client)
    # Create a session so the session_id check passes.
    r = client.post("/tenants/tenant1/projects/p1/sessions",
                    headers=AUTH, json={})
    sid = r.json()["session_id"]
    r2 = client.post(
        "/tenants/tenant1/projects/p1/workflow-runs/ghost/drive",
        headers=AUTH, json={"session_id": sid})
    assert r2.status_code == 404


def test_drive_unauthenticated_401(client):
    _full_setup(client)
    r2 = client.post(
        "/tenants/tenant1/projects/p1/workflow-runs/ghost/drive",
        json={"session_id": "x"})
    assert r2.status_code == 401
