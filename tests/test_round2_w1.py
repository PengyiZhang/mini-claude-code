"""Round 2 Workflow V2 W1 — definition/run separation + storage layer.

Tests the data model + WorkflowService against a real FSStorage, no
HTTP yet (that lands in a follow-up). The storage methods added to
FSStorage are exercised here so any rename or shape change is caught
before HTTP routes are bolted on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.storage import FSStorage
from mini_cc.workflow_v2 import (StepDef, TriggerDef, WorkflowDefinition,
                                  WorkflowRun, WorkflowService)


# ── Storage layer ──────────────────────────────────────────────────────────

def test_save_and_load_workflow_definition_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    d = WorkflowDefinition(
        def_id="wfdef_abc",
        name="nightly",
        description="d",
        version=3,
        steps=[StepDef(id="s1", prompt="do {x}")],
        triggers=[TriggerDef(type="manual")],
        state_schema={"x": {"type": "string"}},
    )
    s.save_workflow_def("p1", d.to_dict())
    raw = s.load_workflow_def("p1", "wfdef_abc")
    assert raw is not None
    assert raw["def_id"] == "wfdef_abc"
    assert raw["version"] == 3
    assert raw["steps"][0]["prompt"] == "do {x}"


def test_load_workflow_def_returns_none_when_missing(tmp_path):
    s = FSStorage(tmp_path / "state")
    assert s.load_workflow_def("p1", "nope") is None


def test_list_workflow_defs_orders_by_updated_at(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow_def("p1", {"def_id": "a", "updated_at": "2026-01-01T00:00:00Z"})
    s.save_workflow_def("p1", {"def_id": "b", "updated_at": "2026-06-01T00:00:00Z"})
    out = s.list_workflow_defs("p1")
    assert [d["def_id"] for d in out] == ["b", "a"]


def test_delete_workflow_def_idempotent(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow_def("p1", {"def_id": "x"})
    assert s.delete_workflow_def("p1", "x") is True
    assert s.delete_workflow_def("p1", "x") is False
    assert s.load_workflow_def("p1", "x") is None


def test_save_and_load_workflow_run(tmp_path):
    s = FSStorage(tmp_path / "state")
    run = WorkflowRun(run_id="wfrun_1", def_id="wfdef_abc", def_version=2)
    s.save_workflow_run("p1", run.to_dict())
    raw = s.load_workflow_run("p1", "wfrun_1")
    assert raw is not None
    assert raw["run_id"] == "wfrun_1"
    assert raw["def_version"] == 2


def test_list_workflow_runs_returns_all(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow_run("p1", {"run_id": "r1", "started_at": "2026-01-01T00:00:00Z"})
    s.save_workflow_run("p1", {"run_id": "r2", "started_at": "2026-06-01T00:00:00Z"})
    out = s.list_workflow_runs("p1")
    assert {r["run_id"] for r in out} == {"r1", "r2"}


def test_workflow_defs_isolated_per_project(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow_def("p1", {"def_id": "shared_id"})
    s.save_workflow_def("p2", {"def_id": "shared_id"})
    assert s.load_workflow_def("p1", "shared_id") is not None
    assert s.load_workflow_def("p2", "shared_id") is not None
    # And listing p1 must not leak p2's def.
    assert all(d["def_id"] == "shared_id" for d in s.list_workflow_defs("p1"))


# ── Service layer ──────────────────────────────────────────────────────────

def _service(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


def test_create_definition_assigns_def_id(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "deploy",
        "steps": [{"id": "s1", "prompt": "go"}],
    })
    assert d.def_id.startswith("wfdef_")
    assert d.version == 1
    # Persisted.
    assert svc.get_definition("p1", d.def_id) is not None


def test_update_definition_bumps_version(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {"name": "v1"})
    updated = svc.update_definition("p1", d.def_id, {"description": "new"})
    assert updated is not None
    assert updated.version == 2
    assert updated.description == "new"
    assert updated.name == "v1"  # unchanged


def test_update_missing_definition_returns_none(tmp_path):
    svc = _service(tmp_path)
    assert svc.update_definition("p1", "ghost", {"name": "x"}) is None


def test_list_definitions_round_trip(tmp_path):
    svc = _service(tmp_path)
    svc.create_definition("p1", {"name": "a"})
    svc.create_definition("p1", {"name": "b"})
    out = svc.list_definitions("p1")
    assert {d.name for d in out} == {"a", "b"}


def test_delete_definition_through_service(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {"name": "x"})
    assert svc.delete_definition("p1", d.def_id) is True
    assert svc.get_definition("p1", d.def_id) is None


def test_start_run_captures_def_version(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "s1", "prompt": "do"}],
    })
    # Bump the def to v2 — the run should still pin the version we ask for.
    d_v2 = svc.update_definition("p1", d.def_id, {"description": "v2"})
    run = svc.start_run("p1", d.def_id)
    assert run is not None
    assert run.def_version == d_v2.version
    assert run.status == "pending"
    # Step runs pre-populated from the definition.
    assert len(run.step_runs) == 1
    assert run.step_runs[0].step_id == "s1"


def test_start_run_with_initial_state_and_trigger(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {"name": "wf",
                                      "steps": [{"id": "s1", "prompt": "hi"}]})
    run = svc.start_run(
        "p1", d.def_id,
        initial_state={"x": "1"},
        trigger={"type": "webhook", "config": {"id": "wh_1"}})
    assert run is not None
    assert run.state == {"x": "1"}
    assert run.trigger["type"] == "webhook"


def test_drive_run_executes_action_steps_and_completes(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "a", "prompt": "step A"},
            {"id": "b", "prompt": "step B uses {a}"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    assert run is not None

    def dispatch(prompt, r):
        return f"ran[{prompt}]"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert finished.step_runs[0].status == "completed"
    assert finished.step_runs[1].status == "completed"
    # The placeholder substitution wired the previous step's output in.
    assert "ran[step B uses ran[step A]]" == finished.step_runs[1].output
    # State captures each step's output keyed by step id.
    assert finished.state["a"] == "ran[step A]"


def test_drive_run_pauses_on_checkpoint_parking_step(tmp_path):
    """A checkpoint step must flip the run to paused and return without
    dispatching. drive_run resumes when called again after the gate is
    externally resolved (the test resolves by swapping the step type)."""
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "pre", "prompt": "pre"},
            {"id": "gate", "type": "checkpoint", "config": {"approvers": ["alice"]}},
            {"id": "post", "prompt": "post"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    assert run is not None

    def dispatch(prompt, r):
        return "ok"

    paused = svc.drive_run("p1", run.run_id, dispatch)
    assert paused.status == "paused"
    # 'pre' completed, 'gate' is paused, 'post' is still pending.
    statuses = {s.step_id: s.status for s in paused.step_runs}
    assert statuses["pre"] == "completed"
    assert statuses["gate"] == "paused"
    assert statuses["post"] == "pending"
    assert paused.current_step_idx == 1  # parked at gate


def test_drive_run_records_failure_and_aborts(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "ok", "prompt": "fine"},
            {"id": "boom", "prompt": "explode"},
        ],
    })
    run = svc.start_run("p1", d.def_id)

    def dispatch(prompt, r):
        if "explode" in prompt:
            raise RuntimeError("nope")
        return "ok"

    failed = svc.drive_run("p1", run.run_id, dispatch)
    assert failed.status == "failed"
    assert failed.step_runs[1].status == "failed"
    assert "RuntimeError" in (failed.step_runs[1].error or "")


def test_drive_run_skips_already_completed_steps_on_resume(tmp_path):
    """Re-invoking drive_run after partial completion picks up where it
    left off and doesn't re-run completed steps."""
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "s1", "prompt": "x"}],
    })
    run = svc.start_run("p1", d.def_id)

    call_count = {"n": 0}

    def dispatch(prompt, r):
        call_count["n"] += 1
        return f"r{call_count['n']}"

    svc.drive_run("p1", run.run_id, dispatch)
    # Second invocation must not re-dispatch.
    svc.drive_run("p1", run.run_id, dispatch)
    assert call_count["n"] == 1


def test_cancel_run_sets_terminal_state(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {"name": "wf",
                                      "steps": [{"id": "s1", "prompt": "x"}]})
    run = svc.start_run("p1", d.def_id)
    cancelled = svc.cancel_run("p1", run.run_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.completed_at is not None


def test_cancel_run_idempotent_on_terminal_states(tmp_path):
    svc = _service(tmp_path)
    d = svc.create_definition("p1", {"name": "wf",
                                      "steps": [{"id": "s1", "prompt": "x"}]})
    run = svc.start_run("p1", d.def_id)
    first = svc.cancel_run("p1", run.run_id)
    second = svc.cancel_run("p1", run.run_id)
    assert first.status == second.status == "cancelled"


def test_list_runs_filters_by_def_id(tmp_path):
    svc = _service(tmp_path)
    d1 = svc.create_definition("p1", {"name": "a", "steps": [{"id": "s", "prompt": "x"}]})
    d2 = svc.create_definition("p1", {"name": "b", "steps": [{"id": "s", "prompt": "x"}]})
    svc.start_run("p1", d1.def_id)
    svc.start_run("p1", d1.def_id)
    svc.start_run("p1", d2.def_id)
    assert len(svc.list_runs("p1", def_id=d1.def_id)) == 2
    assert len(svc.list_runs("p1")) == 3


def test_drive_run_missing_run_raises(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(KeyError):
        svc.drive_run("p1", "ghost", lambda p, r: "")


# ── StepDef / TriggerDef shape ─────────────────────────────────────────────

def test_stepdef_to_dict_includes_all_fields():
    s = StepDef(id="x", type="checkpoint",
                prompt="p", config={"approvers": ["a"]}, condition="state.ok")
    d = s.to_dict()
    assert d["id"] == "x"
    assert d["type"] == "checkpoint"
    assert d["config"]["approvers"] == ["a"]
    assert d["condition"] == "state.ok"


def test_workflow_definition_from_dict_round_trip():
    raw = {
        "def_id": "wfdef_x",
        "name": "x",
        "version": 2,
        "steps": [{"id": "s1", "prompt": "p", "type": "action"}],
        "triggers": [{"type": "webhook", "config": {"id": "wh"}}],
        "state_schema": {"k": {"type": "string"}},
    }
    d = WorkflowDefinition.from_dict(raw)
    again = d.to_dict()
    assert again["def_id"] == "wfdef_x"
    assert again["version"] == 2
    assert again["steps"][0]["prompt"] == "p"
    assert again["triggers"][0]["type"] == "webhook"


def test_workflow_run_from_dict_round_trip():
    raw = {
        "run_id": "r1",
        "def_id": "d",
        "def_version": 1,
        "status": "completed",
        "step_runs": [{"step_id": "s1", "status": "completed", "output": "x"}],
    }
    r = WorkflowRun.from_dict(raw)
    out = r.to_dict()
    assert out["status"] == "completed"
    assert out["step_runs"][0]["output"] == "x"
