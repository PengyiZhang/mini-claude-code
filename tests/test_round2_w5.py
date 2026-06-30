"""W5 — validate step: deterministic state checks, no LLM call."""
from __future__ import annotations

import pytest

from mini_cc.storage import FSStorage
from mini_cc.workflow.workflow_v2 import WorkflowService


def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


# ── Expression-based checks ────────────────────────────────────────────────

def test_validate_passes_when_check_truthy(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "s1", "prompt": "do"},
            {"id": "v", "type": "validate",
             "config": {"check": "len(s1) > 0"}},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "result")
    assert finished.status == "completed"
    v_sr = next(s for s in finished.step_runs if s.step_id == "v")
    assert v_sr.status == "completed"
    assert v_sr.output["valid"] is True


def test_validate_fails_when_check_falsy(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "s1", "prompt": "do"},
            {"id": "v", "type": "validate",
             "config": {"check": "len(s1) > 100"}},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "failed"
    v_sr = next(s for s in finished.step_runs if s.step_id == "v")
    assert v_sr.status == "failed"
    assert "validate failed" in (v_sr.error or "")


def test_validate_check_with_no_state_safe(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate",
                   "config": {"check": "True"}}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "completed"


def test_validate_blocks_unsafe_builtins(tmp_path):
    """eval must not expose open() / __import__ etc."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate",
                   "config": {"check": "open('/etc/passwd')"}}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "failed"
    v_sr = next(s for s in finished.step_runs if s.step_id == "v")
    assert "not defined" in (v_sr.error or "") or "NameError" in (v_sr.error or "")


def test_validate_handles_undefined_var_in_check(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate",
                   "config": {"check": "undefined_var > 5"}}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "failed"


def test_validate_no_check_passes(tmp_path):
    """Validate step without a check is a no-op pass."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate"}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "completed"


# ── Schema-based checks ────────────────────────────────────────────────────

def test_validate_schema_passes_on_match(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "s1", "prompt": "do"},
            {"id": "v", "type": "validate",
             "config": {"check": {"schema": {
                 "type": "object",
                 "required": ["s1"],
             }}}},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "result")
    assert finished.status == "completed"


def test_validate_schema_fails_when_required_missing(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate",
                   "config": {"check": {"schema": {
                       "type": "object",
                       "required": ["nonexistent_key"],
                   }}}}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "failed"
    v_sr = next(s for s in finished.step_runs if s.step_id == "v")
    assert "missing required" in (v_sr.error or "")


def test_validate_schema_type_mismatch(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "s1", "prompt": "do"},
            {"id": "v", "type": "validate",
             "config": {"check": {"schema": {"type": "integer"}}}},
        ],
    })
    # State is a dict (run.state) so type check against 'integer' fails.
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    assert finished.status == "failed"


# ── Combined flows ─────────────────────────────────────────────────────────

def test_validate_between_actions_resumes_correctly(tmp_path):
    """A passing validate between two action steps doesn't break drive."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [
            {"id": "a", "prompt": "do A"},
            {"id": "v", "type": "validate",
             "config": {"check": "len(a) > 0"}},
            {"id": "b", "prompt": "do B with {a}"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: f"ran[{p}]")
    assert finished.status == "completed"
    statuses = {s.step_id: s.status for s in finished.step_runs}
    assert statuses == {"a": "completed", "v": "completed", "b": "completed"}
    assert "ran[do A]" in finished.step_runs[2].output


def test_validate_output_captures_detail(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "v", "type": "validate",
                   "config": {"check": "True"}}],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "x")
    v_sr = next(s for s in finished.step_runs if s.step_id == "v")
    assert v_sr.output["valid"] is True
    assert "check=" in v_sr.output["detail"]
