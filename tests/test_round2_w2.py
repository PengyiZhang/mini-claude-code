"""W2 — checkpoint gate resolution: service layer + HTTP route.

Verifies that resolve_gate advances a parked checkpoint on approve,
fails it on reject, enforces approver authorization, and the HTTP
route maps errors to the right status codes.
"""
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


# ── Service-layer tests ────────────────────────────────────────────────────

def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


def _park_at_checkpoint(svc, project_id="p1", *, approvers=None):
    """Create a def with a checkpoint step and drive to the parked state."""
    cfg = {"approvers": approvers} if approvers else {}
    d = svc.create_definition(project_id, {
        "name": "wf",
        "steps": [
            {"id": "pre", "prompt": "pre"},
            {"id": "gate", "type": "checkpoint", "config": cfg},
            {"id": "post", "prompt": "post uses {gate}"},
        ],
    })
    run = svc.start_run(project_id, d.def_id)
    run = svc.drive_run(project_id, run.run_id, lambda p, r: f"ran[{p}]")
    return d, run


def test_resolve_gate_approve_advances_run(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    # Pre-flight: gate is parked.
    assert run.status == "paused"
    assert run.step_runs[1].status == "paused"

    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="approve",
                                 approver="alice",
                                 feedback="ship it")
    assert resolved is not None
    assert resolved.status == "paused"  # still paused until next drive
    gate_sr = next(s for s in resolved.step_runs if s.step_id == "gate")
    assert gate_sr.status == "completed"
    assert gate_sr.output["decision"] == "approve"
    assert gate_sr.output["approver"] == "alice"
    assert gate_sr.output["feedback"] == "ship it"
    # current_step_idx advances past the gate.
    assert resolved.current_step_idx == 2
    # State captures the gate output keyed by step id.
    assert resolved.state["gate"]["decision"] == "approve"


def test_resolve_gate_approve_then_drive_completes_run(tmp_path):
    """After approve, drive_run resumes and finishes the remaining steps."""
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    svc.resolve_gate("p1", run.run_id, "gate",
                      decision="approve", approver="alice")

    def dispatch(prompt, r):
        return "ok"
    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    # 'post' ran after the gate was satisfied.
    post_sr = next(s for s in finished.step_runs if s.step_id == "post")
    assert post_sr.status == "completed"
    # {gate} substitution worked — post prompt referenced gate output.
    assert "ok" == post_sr.output


def test_resolve_gate_reject_fails_run(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="reject",
                                 approver="bob",
                                 feedback="bad")
    assert resolved is not None
    assert resolved.status == "failed"
    gate_sr = next(s for s in resolved.step_runs if s.step_id == "gate")
    assert gate_sr.status == "failed"
    assert gate_sr.error == "bad"
    # Subsequent steps stay pending — they don't get auto-failed.
    post_sr = next(s for s in resolved.step_runs if s.step_id == "post")
    assert post_sr.status == "pending"


def test_resolve_gate_reject_uses_default_error_when_no_feedback(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="reject", approver="bob")
    gate_sr = next(s for s in resolved.step_runs if s.step_id == "gate")
    assert gate_sr.error == "rejected by approver"


def test_resolve_gate_approve_completes_run_when_gate_is_last_step(tmp_path):
    """If the gate is the final step, approve completes the run outright."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "gate", "type": "checkpoint"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    svc.drive_run("p1", run.run_id, lambda p, r: "x")
    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="approve", approver="a")
    assert resolved.status == "completed"
    assert resolved.completed_at is not None


def test_resolve_gate_rejects_unauthorized_approver(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc, approvers=["alice", "carol"])
    with pytest.raises(ValueError, match="approver"):
        svc.resolve_gate("p1", run.run_id, "gate",
                          decision="approve", approver="bob")


def test_resolve_gate_allows_authorized_approver(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc, approvers=["alice", "carol"])
    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="approve", approver="carol")
    assert resolved.step_runs[1].status == "completed"


def test_resolve_gate_non_gate_step_raises(tmp_path):
    """Calling resolve on an action step must fail loudly — it's not a gate."""
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    with pytest.raises(ValueError, match="not a gate step"):
        svc.resolve_gate("p1", run.run_id, "pre",
                          decision="approve", approver="a")


def test_resolve_gate_unknown_step_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    with pytest.raises(ValueError, match="not a gate step"):
        svc.resolve_gate("p1", run.run_id, "ghost",
                          decision="approve", approver="a")


def test_resolve_gate_step_not_paused_raises(tmp_path):
    """Resolving an already-resolved gate is a no-op error — the resolver
    shouldn't be able to double-advance."""
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    svc.resolve_gate("p1", run.run_id, "gate",
                      decision="approve", approver="a")
    with pytest.raises(ValueError, match="not paused"):
        svc.resolve_gate("p1", run.run_id, "gate",
                          decision="approve", approver="a")


def test_resolve_gate_invalid_decision_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)
    with pytest.raises(ValueError, match="decision"):
        svc.resolve_gate("p1", run.run_id, "gate",
                          decision="maybe", approver="a")


def test_resolve_gate_run_missing_returns_none(tmp_path):
    svc = _svc(tmp_path)
    assert svc.resolve_gate("p1", "ghost", "gate",
                             decision="approve", approver="a") is None


def test_resolve_gate_persists_after_call(tmp_path):
    """State changes survive a fresh load — important so a server restart
    doesn't drop the resolution."""
    s = FSStorage(tmp_path / "state")
    svc = WorkflowService(s)
    d, run = _park_at_checkpoint(svc)
    svc.resolve_gate("p1", run.run_id, "gate",
                      decision="approve", approver="a")
    fresh = WorkflowService(s)
    reloaded = fresh.get_run("p1", run.run_id)
    assert reloaded is not None
    gate_sr = next(s for s in reloaded.step_runs if s.step_id == "gate")
    assert gate_sr.status == "completed"


def test_resolve_gate_no_approvers_allows_anyone(tmp_path):
    """Empty approvers list = anyone may resolve (auth enforced at HTTP)."""
    svc = _svc(tmp_path)
    d, run = _park_at_checkpoint(svc)  # no approvers configured
    resolved = svc.resolve_gate("p1", run.run_id, "gate",
                                 decision="approve", approver="random")
    assert resolved.step_runs[1].status == "completed"


# ── HTTP route tests ───────────────────────────────────────────────────────

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


def _park_via_service(client, tmp_path, *, approvers=None):
    """Use the underlying project's WorkflowService to set up a parked
    checkpoint, then the HTTP layer resolves it."""
    pm = client.app.dependency_overrides.get(  # type: ignore[attr-defined]
        lambda: None)  # never matches; just to access pm via app state
    # Easier: reach into app state directly.
    pm = client.app.state.pm  # type: ignore[attr-defined]
    project = pm.get("p1")
    svc = project.workflows_v2
    cfg = {"approvers": approvers} if approvers else {}
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "pre", "prompt": "pre"},
            {"id": "gate", "type": "checkpoint", "config": cfg},
            {"id": "post", "prompt": "post"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    svc.drive_run("p1", run.run_id, lambda p, r: "ok")
    return run.run_id


def test_http_resolve_approve(client, tmp_path):
    run_id = _park_via_service(client, tmp_path)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH,
        json={"approver": "alice", "decision": "approve", "feedback": "ok"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused"
    gate = next(s for s in body["step_runs"] if s["step_id"] == "gate")
    assert gate["status"] == "completed"
    assert gate["output"]["approver"] == "alice"


def test_http_resolve_reject(client, tmp_path):
    run_id = _park_via_service(client, tmp_path)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH,
        json={"approver": "bob", "decision": "reject", "feedback": "no"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"


def test_http_resolve_unknown_run_404(client, tmp_path):
    r = client.post(
        "/tenants/tenant1/projects/p1/workflow-runs/ghost/steps/gate/resolve",
        headers=AUTH,
        json={"decision": "approve"})
    assert r.status_code == 404


def test_http_resolve_non_gate_step_400(client, tmp_path):
    run_id = _park_via_service(client, tmp_path)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/pre/resolve",
        headers=AUTH,
        json={"decision": "approve"})
    assert r.status_code == 400


def test_http_resolve_invalid_decision_400(client, tmp_path):
    run_id = _park_via_service(client, tmp_path)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH,
        json={"decision": "maybe"})
    assert r.status_code == 400


def test_http_resolve_unauthorized_approver_400(client, tmp_path):
    run_id = _park_via_service(client, tmp_path, approvers=["alice"])
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH,
        json={"approver": "bob", "decision": "approve"})
    assert r.status_code == 400


def test_http_resolve_unauthenticated_401(client, tmp_path):
    run_id = _park_via_service(client, tmp_path)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        json={"decision": "approve"})
    assert r.status_code == 401


def test_http_resolve_idempotent_reject(client, tmp_path):
    """Resolving an already-resolved gate returns 400 (not 500 from a
    double-advance)."""
    run_id = _park_via_service(client, tmp_path)
    r1 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH, json={"decision": "approve", "approver": "a"})
    assert r1.status_code == 200
    r2 = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/steps/gate/resolve",
        headers=AUTH, json={"decision": "approve", "approver": "a"})
    assert r2.status_code == 400
