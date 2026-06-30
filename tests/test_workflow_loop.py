"""Workflow V2 — loop step type.

A ``loop`` step repeats a sequence of body steps while a condition
holds. Config:

- ``body``: list of step ids to execute each iteration, in order.
- ``while``: Python expression evaluated against ``{**state, "iter": n}``
  where ``n`` is the iteration count starting at 0. Default ``"True"``
  (rely on ``max_iterations`` to terminate).
- ``max_iterations``: int, default 100. Caps the loop to prevent
  runaway workflows.
- ``counter_var``: state key for the iteration count, default
  ``f"{step_id}_iter"``.

Each iteration:
1. Evaluate ``while`` with current iter count.
2. If falsy or iter >= max_iterations → exit loop, advance to linear next.
3. Otherwise → execute each body step (must be non-parking:
   action / validate / branch only; parking steps raise).
4. Increment counter_var in state.

The loop's step_run.output records the final iteration count.
"""
from __future__ import annotations

import pytest

from mini_cc.storage import FSStorage
from mini_cc.workflow.workflow_v2 import WorkflowService


def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


def test_loop_runs_body_n_times_then_exits(tmp_path):
    """while="iter < 3" runs body 3 times then continues to the next step."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "loop_start", "type": "loop",
             "config": {
                 "body": ["do_work"],
                 "while": "iter < 3",
             }},
            {"id": "do_work", "prompt": "working"},
            {"id": "after", "prompt": "after loop"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(prompt: str, _r) -> str:
        calls.append(prompt)
        return "ok"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    # Body ran 3 times + after ran once.
    work_calls = sum(1 for c in calls if c == "working")
    assert work_calls == 3
    assert "after loop" in calls
    # Counter persisted in state.
    assert finished.state["loop_start_iter"] == 3


def test_loop_max_iterations_caps_runaway_loops(tmp_path):
    """while="True" with max_iterations=5 stops after 5 iterations."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {
                 "body": ["work"],
                 "while": "True",
                 "max_iterations": 5,
             }},
            {"id": "work", "prompt": "w"},
            {"id": "tail", "prompt": "t"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert calls.count("w") == 5
    assert finished.state["lp_iter"] == 5
    # tail still runs after the capped loop.
    assert "t" in calls


def test_loop_with_falsy_while_exits_immediately(tmp_path):
    """while="False" runs the body zero times and falls through."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["work"], "while": "False"}},
            {"id": "work", "prompt": "should not run"},
            {"id": "tail", "prompt": "tail"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    assert calls == ["tail"]
    assert finished.state["lp_iter"] == 0


def test_loop_records_iteration_count_in_step_run_output(tmp_path):
    """The loop step's own StepRun.output captures the iteration count."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["w"], "while": "iter < 2"}},
            {"id": "w", "prompt": "w"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "ok")
    lp_sr = next(s for s in finished.step_runs if s.step_id == "lp")
    assert lp_sr.status == "completed"
    assert lp_sr.output["iterations"] == 2


def test_loop_body_with_multiple_steps_runs_in_order(tmp_path):
    """Body [a, b, c] runs as a → b → c each iteration."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["a", "b"], "while": "iter < 2"}},
            {"id": "a", "prompt": "alpha"},
            {"id": "b", "prompt": "beta"},
            {"id": "after", "prompt": "done"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    # Two iterations of [alpha, beta], then done.
    assert calls == ["alpha", "beta", "alpha", "beta", "done"]


def test_loop_body_can_reference_iter_via_state_substitution(tmp_path):
    """Body steps see the current iteration via {lp_iter} placeholder."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["w"], "while": "iter < 3"}},
            {"id": "w", "prompt": "iter={lp_iter}"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    # Each iteration sees the iter counter BEFORE incrementing, so
    # iterations 0, 1, 2 produce "iter=0", "iter=1", "iter=2".
    assert calls == ["iter=0", "iter=1", "iter=2"]


def test_loop_body_with_parking_step_raises(tmp_path):
    """A loop body containing a checkpoint / webhook_wait / email_wait
    can't be executed synchronously; drive_run fails the run with a
    clear error rather than silently parking inside a loop."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["gate"], "while": "iter < 3"}},
            {"id": "gate", "type": "checkpoint"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    finished = svc.drive_run("p1", run.run_id, lambda p, r: "ok")
    assert finished.status == "failed"
    lp_sr = next(s for s in finished.step_runs if s.step_id == "lp")
    assert lp_sr.status == "failed"
    assert "parking" in (lp_sr.error or "").lower() or \
           "checkpoint" in (lp_sr.error or "").lower()


def test_loop_with_condition_gated_body_step(tmp_path):
    """Inside a loop body, individual steps' `condition` field provides
    per-iteration branching. iter 0 runs the warmup; later iters skip it."""
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "loop",
        "steps": [
            {"id": "lp", "type": "loop",
             "config": {"body": ["warmup", "main"], "while": "iter < 3"}},
            {"id": "warmup", "prompt": "warmup",
             "condition": "lp_iter == 0"},
            {"id": "main", "prompt": "main iter {lp_iter}"},
            {"id": "tail", "prompt": "tail"},
        ],
    })
    run = svc.start_run("p1", d.def_id)
    calls: list[str] = []

    def dispatch(p: str, _r) -> str:
        calls.append(p)
        return "x"

    finished = svc.drive_run("p1", run.run_id, dispatch)
    assert finished.status == "completed"
    # iter 0: warmup + main; iter 1, 2: main only; then tail.
    assert calls == ["warmup", "main iter 0", "main iter 1", "main iter 2", "tail"]
