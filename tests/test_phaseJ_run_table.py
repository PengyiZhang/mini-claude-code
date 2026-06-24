"""Phase J — Run Table REST endpoints (workflows + background read views)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.tools.background import BackgroundScheduler, _BGTask
from mini_cc.workflow import workflow_from_dict

AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def app_env(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    # Create a warm session whose loop we can inject state onto.
    r = client.post("/tenants/tenant1/projects/p1/sessions",
                    headers=AUTH, json={"session_id": "sess1"})
    assert r.status_code in (200, 201)
    return client, pm, sm


def _loop(sm):
    return sm._sessions[("p1", "sess1")].loop


def test_get_workflows_empty(app_env):
    c, pm, sm = app_env
    r = c.get("/tenants/tenant1/projects/p1/sessions/sess1/run-table/workflows",
              headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is None
    assert body["saved"] == []


def test_get_workflows_returns_active_and_saved(app_env):
    c, pm, sm = app_env
    ref = _loop(sm).project
    wf = workflow_from_dict({
        "name": "demo",
        "description": "demo wf",
        "steps": [{"id": "s1", "prompt": "do X"}],
    })
    wf.results["s1"] = "ok"
    # workflow tools attach active_workflow onto the ProjectRef instance.
    setattr(ref, "active_workflow", wf)
    # Persist a saved workflow via the project's storage (disk).
    ref.storage.save_workflow("p1", wf.to_dict())

    r = c.get("/tenants/tenant1/projects/p1/sessions/sess1/run-table/workflows",
              headers=AUTH)
    body = r.json()
    assert body["active"] is not None
    assert body["active"]["name"] == "demo"
    assert body["active"]["results"] == {"s1": "ok"}
    assert len(body["saved"]) == 1
    assert body["saved"][0]["name"] == "demo"


def test_get_background_empty_without_scheduler(app_env):
    c, pm, sm = app_env
    ref = _loop(sm).project
    # Detach the scheduler → endpoint returns [].
    ref.background = None
    r = c.get("/tenants/tenant1/projects/p1/sessions/sess1/run-table/background",
              headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_get_background_lists_session_tasks(app_env):
    c, pm, sm = app_env
    ref = _loop(sm).project
    bg = BackgroundScheduler()
    bg._tasks["bg_a"] = _BGTask("bg_a", "tu1", "bash",
                                "echo first", status="running")
    bg._tasks["bg_b"] = _BGTask("bg_b", "tu2", "bash",
                                "echo second", status="completed")
    ref.background = bg

    r = c.get("/tenants/tenant1/projects/p1/sessions/sess1/run-table/background",
              headers=AUTH)
    tasks = r.json()
    assert len(tasks) == 2
    assert {t["bg_id"] for t in tasks} == {"bg_a", "bg_b"}
    assert {t["status"] for t in tasks} == {"running", "completed"}


def test_run_table_unknown_session_404(app_env):
    c, pm, sm = app_env
    r = c.get("/tenants/tenant1/projects/p1/sessions/ghost/run-table/workflows",
              headers=AUTH)
    assert r.status_code == 404


def test_run_table_unknown_project_404(app_env):
    c, pm, sm = app_env
    r = c.get("/tenants/tenant1/projects/ghost/sessions/sess1/run-table/workflows",
              headers=AUTH)
    assert r.status_code == 404
