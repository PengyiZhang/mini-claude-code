"""Workflow V2 — branch step type.

A ``branch`` step evaluates a list of ``(when, next)`` cases against the
run's state and jumps to the first matching ``next`` step id. With no
match it falls through to ``config.default_next`` if set, otherwise to
the linear next step. The step records its decision in
``run.state[step_id]`` so downstream steps and the UI can see which
branch fired.

This is the W7 extension — the existing ``condition`` field is a soft
"skip this step" flag, while ``branch`` is an explicit "jump elsewhere"
construct that enables if/else ladders and backward gotos for loops.
"""
from __future__ import annotations

from mini_cc.storage import FSStorage
from mini_cc.workflow.workflow_v2 import WorkflowService


def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


# ── Branch step semantics ─────────────────────────────────────────────────

def test_branch_jumps_to_first_matching_case(tmp_path):
    """First truthy `when` wins; execution jumps to its `next` step id.
    Each branch target sets `next: end` so the other case is skipped."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch",
             "config": {"branches": [
                 {"when": "x < 10", "next": "small"},
                 {"when": "x >= 10", "next": "large"},
             ]}},
            {"id": "small", "prompt": "small branch", "next": "end"},
            {"id": "large", "prompt": "large branch", "next": "end"},
            {"id": "end", "prompt": "fin"},
        ],
    })
    run = svc.start_run("p1", d.def_id, initial_state={"x": 5})
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return f"ran[{p}]"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert "small branch" in calls
    assert "large branch" not in calls
    assert "fin" in calls


def test_branch_falls_through_when_no_case_matches(tmp_path):
    """No match and no default_next → fall through to linear next step.
    The fall-through step's own `next` controls where execution goes
    after."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch",
             "config": {"branches": [
                 {"when": "x > 100", "next": "rare"},
             ]}},
            {"id": "common", "prompt": "common path", "next": "end"},
            {"id": "rare", "prompt": "rare path", "next": "end"},
            {"id": "end", "prompt": "fin"},
        ],
    })
    run = svc.start_run("p1", d.def_id, initial_state={"x": 5})
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert "common path" in calls
    assert "rare path" not in calls


def test_branch_default_next_used_on_no_match(tmp_path):
    """config.default_next overrides the linear fall-through."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch",
             "config": {
                 "branches": [{"when": "x > 100", "next": "rare"}],
                 "default_next": "fallback",
             }},
            {"id": "linear_next", "prompt": "should be skipped", "next": "end"},
            {"id": "fallback", "prompt": "default target", "next": "end"},
            {"id": "rare", "prompt": "rare", "next": "end"},
            {"id": "end", "prompt": "fin"},
        ],
    })
    run = svc.start_run("p1", d.def_id, initial_state={"x": 5})
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert "default target" in calls
    assert "should be skipped" not in calls
    assert "rare" not in calls


def test_branch_records_decision_in_state(tmp_path):
    """The chosen target is recorded in run.state so later steps can
    inspect it via `{decide}` substitution."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch",
             "config": {"branches": [
                 {"when": "x < 10", "next": "small"},
             ]}},
            {"id": "small", "prompt": "picked {decide}"},
            {"id": "large", "prompt": "should not run"},
        ],
    })
    run = svc.start_run("p1", d.def_id, initial_state={"x": 5})
    finished = svc.drive_run("p1", run.run_id, lambda p, r: f"ran[{p}]")
    # branch decision is recorded as the chosen step id
    assert finished.state["decide"]["branch_taken"] == "small"
    # downstream step saw the substituted value
    small_sr = next(s for s in finished.step_runs if s.step_id == "small")
    assert "picked" in small_sr.output
    assert "small" in small_sr.output


def test_branch_step_does_not_dispatch(tmp_path):
    """Branch is a pure control-flow step; dispatch_fn is never called."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch",
             "config": {"branches": [
                 {"when": "True", "next": "tail"},
             ]}},
            {"id": "skipped", "prompt": "should not run"},
            {"id": "tail", "prompt": "tail"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(prompt: str, _run) -> str:
        calls.append(prompt)
        return f"ran[{prompt}]"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    # Only "tail" was dispatched; branch and skipped produced no calls.
    assert calls == ["tail"]
    assert finished.status == "completed"


def test_branch_with_no_branches_falls_through(tmp_path):
    """A branch step with empty branches config is a no-op pass-through."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "branch",
        "steps": [
            {"id": "decide", "type": "branch", "config": {}},
            {"id": "next", "prompt": "next"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "ok")
    assert finished.status == "completed"
    decide_sr = next(s for s in finished.step_runs if s.step_id == "decide")
    assert decide_sr.status == "completed"
    # Falls through to "next" linearly.
    next_sr = next(s for s in finished.step_runs if s.step_id == "next")
    assert next_sr.status == "completed"
