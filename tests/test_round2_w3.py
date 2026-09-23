"""W3 — inbound webhook resolver: service layer + HTTP route."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.storage import FSStorage
from mini_cc.workflow.workflow_v2 import WorkflowService


AUTH = {"Authorization": "Bearer mck_testkey"}


def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


def _park_at_webhook_wait(svc, project_id="p1", *, webhook_id=None,
                           event_filter=None):
    cfg = {}
    if webhook_id:
        cfg["webhook_id"] = webhook_id
    if event_filter:
        cfg["event_filter"] = event_filter
    d = svc.create_definition(project_id, {
        "name": "wf",
        "steps": [
            {"id": "pre", "prompt": "pre"},
            {"id": "wait", "type": "webhook_wait", "config": cfg},
            {"id": "post", "prompt": "post uses {wait}"},
        ],
    })
    run = svc.start_run(project_id, d.def_id)
    run = svc.drive_run(project_id, run.run_id, lambda p, r: f"ran[{p}]")
    return d, run


# ── Service layer ──────────────────────────────────────────────────────────

def test_resolve_webhook_wait_advances_run(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc)
    assert run.status == "paused"
    resolved = svc.resolve_webhook_wait(
        "p1", run.run_id, "wait",
        {"event": "build.done", "data": {"pr": 42}})
    assert resolved is not None
    assert resolved.current_step_idx == 2
    wait_sr = next(s for s in resolved.step_runs if s.step_id == "wait")
    assert wait_sr.status == "completed"
    assert wait_sr.output["payload"]["data"]["pr"] == 42
    assert wait_sr.output["resolver"] == "webhook"
    assert resolved.state["wait"]["payload"]["event"] == "build.done"


def test_resolve_webhook_wait_then_drive_completes(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc)
    svc.resolve_webhook_wait("p1", run.run_id, "wait",
                              {"event": "x", "data": {}})
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "post-result")
    assert finished.status == "completed"
    post_sr = next(s for s in finished.step_runs if s.step_id == "post")
    assert post_sr.status == "completed"
    assert post_sr.output == "post-result"


def test_resolve_webhook_wait_event_filter_match(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc, event_filter="build.success")
    resolved = svc.resolve_webhook_wait(
        "p1", run.run_id, "wait",
        {"event": "build.success", "data": {}})
    assert resolved.step_runs[1].status == "completed"


def test_resolve_webhook_wait_event_filter_mismatch_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc, event_filter="build.success")
    with pytest.raises(ValueError, match="does not match filter"):
        svc.resolve_webhook_wait(
            "p1", run.run_id, "wait",
            {"event": "build.failed", "data": {}})


def test_resolve_webhook_wait_non_webhook_step_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc)
    with pytest.raises(ValueError, match="not a webhook_wait"):
        svc.resolve_webhook_wait("p1", run.run_id, "pre", {})


def test_resolve_webhook_wait_not_paused_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_webhook_wait(svc)
    svc.resolve_webhook_wait("p1", run.run_id, "wait", {"event": "x"})
    with pytest.raises(ValueError, match="not paused"):
        svc.resolve_webhook_wait("p1", run.run_id, "wait", {"event": "x"})


def test_resolve_webhook_wait_completes_when_last_step(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "wait", "type": "webhook_wait"}],
    })
    run = svc.start_run("p1", d.def_id)
    svc.drive_run("p1", run.run_id, lambda p, r: "x")
    resolved = svc.resolve_webhook_wait("p1", run.run_id, "wait", {"x": 1})
    assert resolved.status == "completed"


def test_resolve_webhook_wait_run_missing_returns_none(tmp_path):
    svc = _svc(tmp_path)
    assert svc.resolve_webhook_wait("p1", "ghost", "wait", {}) is None


def test_resolve_webhook_wait_persists(tmp_path):
    s = FSStorage(tmp_path / "state")
    svc = WorkflowService(s)
    d, run = _park_at_webhook_wait(svc)
    svc.resolve_webhook_wait("p1", run.run_id, "wait", {"event": "x"})
    fresh = WorkflowService(s)
    reloaded = fresh.get_run("p1", run.run_id)
    assert reloaded.step_runs[1].status == "completed"


# ── HTTP route ─────────────────────────────────────────────────────────────

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
    c = TestClient(app)
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    return c


def _park_via_service(client, *, webhook_id=None, event_filter=None):
    pm = client.app.state.pm
    project = pm.get("p1", tenant_id="tenant1")
    svc = project.workflows_v2
    cfg = {}
    if webhook_id:
        cfg["webhook_id"] = webhook_id
    if event_filter:
        cfg["event_filter"] = event_filter
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "wait", "type": "webhook_wait", "config": cfg},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    svc.drive_run("p1", run.run_id, lambda p, r: "x")
    return run.run_id


def test_http_resolve_webhook_no_secret_required_when_unconfigured(client):
    """No webhook_id in step config → no query param needed."""
    run_id = _park_via_service(client)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait",
        json={"event": "build.done", "data": {"pr": 7}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["step_runs"][0]["status"] == "completed"
    assert body["step_runs"][0]["output"]["payload"]["data"]["pr"] == 7


def test_http_resolve_webhook_requires_matching_webhook_id(client):
    run_id = _park_via_service(client, webhook_id="wh_secret_123")
    # Missing query param → 401.
    r1 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait",
        json={"event": "x"})
    assert r1.status_code == 401
    # Wrong value → 401.
    r2 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait",
        params={"webhook_id": "wrong"}, json={"event": "x"})
    assert r2.status_code == 401
    # Correct value → 200.
    r3 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait",
        params={"webhook_id": "wh_secret_123"},
        json={"event": "x"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["step_runs"][0]["status"] == "completed"


def test_http_resolve_webhook_unknown_run_404(client):
    r = client.post(
        "/tenants/tenant1/projects/p1/workflow-runs/ghost/webhook/wait",
        json={})
    assert r.status_code == 404


def test_http_resolve_webhook_non_webhook_step_404(client):
    """Hitting /webhook/{step_id} on an action step surfaces 404,
    not 400 — the endpoint is webhook-specific."""
    pm = client.app.state.pm
    project = pm.get("p1", tenant_id="tenant1")
    svc = project.workflows_v2
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "pre", "prompt": "x"}],
    })
    run = svc.start_run("p1", d.def_id)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run.run_id}/webhook/pre",
        json={})
    assert r.status_code == 404


def test_http_resolve_webhook_event_filter_mismatch_400(client):
    run_id = _park_via_service(client, event_filter="build.success")
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait",
        json={"event": "build.failed"})
    assert r.status_code == 400


def test_http_resolve_webhook_no_body_works(client):
    """Empty body is fine — webhook may just signal 'done'."""
    run_id = _park_via_service(client)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/webhook/wait")
    assert r.status_code == 200, r.text
    body = r.json()
    # payload reflects empty dict.
    assert body["step_runs"][0]["output"]["payload"] == {}
