"""Workflow V2 — Definition/Run concept separation.

W1 backend foundation. The legacy Workflow class mixes "what to run"
(template) with "what happened" (state + results). This module adds:

- WorkflowDefinition: a versioned template (steps + triggers + state
  schema). Mutated via CRUD; immutable once a run references it.
- WorkflowRun: a single execution. Created from a definition, persists
  state transitions, exposes an event log for UI / replay.
- WorkflowService: ties storage + dispatch together. The agent's
  AgentLoop drives action steps; the service handles state machine
  transitions and event log appends.

The legacy tools/workflow.py API stays untouched — new code lives
under /workflows HTTP routes and the WorkflowService entry point.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


# ── Step definitions ───────────────────────────────────────────────────────

@dataclass
class StepDef:
    """One step of a workflow definition.

    Step types (W1 supports ``action`` only; W2-W5 add the rest):
    - ``action``        — sub-prompt dispatched through AgentLoop
    - ``validate``      — JSONPath/predicate check (W5)
    - ``checkpoint``    — pause for human approval (W2)
    - ``webhook_wait``  — pause for inbound webhook (W3)
    - ``email_wait``    — pause for inbound email (W4)
    """
    id: str
    type: str = "action"
    prompt: str = ""
    # JSON-schema-style dicts; validated by the runner before/after.
    inputs_schema: dict = field(default_factory=dict)
    outputs_schema: dict = field(default_factory=dict)
    # Free-form config — type-specific knobs (approvers, webhook_id,
    # timeout_sec, etc.) live here so StepDef stays a single class.
    config: dict = field(default_factory=dict)
    # Soft branch: skip this step when the expression is falsy.
    condition: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "prompt": self.prompt,
            "inputs_schema": dict(self.inputs_schema),
            "outputs_schema": dict(self.outputs_schema),
            "config": dict(self.config),
            "condition": self.condition,
        }


@dataclass
class TriggerDef:
    """How a new run can be started.

    W1: only ``manual`` is honored. The other types are parsed and
    stored so definitions authored ahead of the feature don't lose
    their triggers when W3/W4 land.
    """
    type: str  # manual | webhook | schedule | email
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "config": dict(self.config)}


# ── Definition ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowDefinition:
    """Versioned workflow template.

    Definitions are mutable via PUT (which bumps version). A Run
    captures ``def_version`` at creation time so it stays reproducible
    even as the definition evolves.
    """
    def_id: str
    name: str
    description: str = ""
    version: int = 1
    owner: str | None = None
    created_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    steps: list[StepDef] = field(default_factory=list)
    triggers: list[TriggerDef] = field(default_factory=list)
    state_schema: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "def_id": self.def_id, "name": self.name,
            "description": self.description, "version": self.version,
            "owner": self.owner,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "triggers": [t.to_dict() for t in self.triggers],
            "state_schema": dict(self.state_schema),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkflowDefinition":
        return cls(
            def_id=raw["def_id"],
            name=raw.get("name", raw["def_id"]),
            description=raw.get("description", ""),
            version=int(raw.get("version", 1)),
            owner=raw.get("owner"),
            created_at=raw.get("created_at") or _iso_now(),
            updated_at=raw.get("updated_at") or _iso_now(),
            steps=[StepDef(
                id=s["id"],
                type=s.get("type", "action"),
                prompt=s.get("prompt", ""),
                inputs_schema=s.get("inputs_schema", {}),
                outputs_schema=s.get("outputs_schema", {}),
                config=s.get("config", {}),
                condition=s.get("condition"),
            ) for s in raw.get("steps", [])],
            triggers=[TriggerDef(
                type=t.get("type", "manual"),
                config=t.get("config", {}),
            ) for t in raw.get("triggers", [])],
            state_schema=raw.get("state_schema", {}),
        )


# ── Run ────────────────────────────────────────────────────────────────────

@dataclass
class StepRun:
    """Per-step execution record. Appended to a run as steps complete."""
    step_id: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    started_at: str | None = None
    completed_at: str | None = None
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id, "status": self.status,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "output": self.output, "error": self.error,
        }


@dataclass
class WorkflowRun:
    """One execution of a WorkflowDefinition."""
    run_id: str
    def_id: str
    def_version: int
    status: str = "pending"  # pending|running|paused|completed|failed|cancelled
    current_step_idx: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    state: dict = field(default_factory=dict)
    step_runs: list[StepRun] = field(default_factory=list)
    # Trigger that started this run (echoed for audit).
    trigger: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "def_id": self.def_id,
            "def_version": self.def_version, "status": self.status,
            "current_step_idx": self.current_step_idx,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "state": dict(self.state),
            "step_runs": [s.to_dict() for s in self.step_runs],
            "trigger": dict(self.trigger),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkflowRun":
        return cls(
            run_id=raw["run_id"],
            def_id=raw["def_id"],
            def_version=int(raw.get("def_version", 1)),
            status=raw.get("status", "pending"),
            current_step_idx=int(raw.get("current_step_idx", 0)),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            state=raw.get("state", {}),
            step_runs=[StepRun(
                step_id=s["step_id"], status=s.get("status", "pending"),
                started_at=s.get("started_at"),
                completed_at=s.get("completed_at"),
                output=s.get("output"), error=s.get("error"),
            ) for s in raw.get("step_runs", [])],
            trigger=raw.get("trigger", {}),
        )


# ── Service ────────────────────────────────────────────────────────────────

class WorkflowService:
    """Orchestrates definitions + runs over a Storage backend.

    Dispatch protocol: ``dispatch_fn(step_prompt, run) -> str`` returns
    the agent's response for an ``action`` step. Service owns state
    transitions + persistence; the dispatcher just runs the prompt.
    """

    def __init__(self, storage):
        self.storage = storage

    # ── Definitions CRUD ──────────────────────────────────────────────────
    def create_definition(self, project_id: str, payload: dict,
                          *, def_id: str | None = None) -> WorkflowDefinition:
        def_id = def_id or f"wfdef_{uuid.uuid4().hex[:10]}"
        payload = {**payload, "def_id": def_id}
        d = WorkflowDefinition.from_dict(payload)
        self.storage.save_workflow_def(project_id, d.to_dict())
        return d

    def get_definition(self, project_id: str, def_id: str) -> WorkflowDefinition | None:
        raw = self.storage.load_workflow_def(project_id, def_id)
        return WorkflowDefinition.from_dict(raw) if raw else None

    def list_definitions(self, project_id: str) -> list[WorkflowDefinition]:
        return [WorkflowDefinition.from_dict(r)
                for r in self.storage.list_workflow_defs(project_id)]

    def update_definition(self, project_id: str, def_id: str,
                          payload: dict) -> WorkflowDefinition | None:
        existing = self.get_definition(project_id, def_id)
        if existing is None:
            return None
        merged = existing.to_dict()
        merged.update(payload)
        merged["def_id"] = def_id  # never let the id change
        merged["version"] = existing.version + 1
        merged["updated_at"] = _iso_now()
        d = WorkflowDefinition.from_dict(merged)
        self.storage.save_workflow_def(project_id, d.to_dict())
        return d

    def delete_definition(self, project_id: str, def_id: str) -> bool:
        return self.storage.delete_workflow_def(project_id, def_id)

    # ── Run management ────────────────────────────────────────────────────
    def start_run(self, project_id: str, def_id: str, *,
                  initial_state: dict | None = None,
                  trigger: dict | None = None) -> WorkflowRun | None:
        d = self.get_definition(project_id, def_id)
        if d is None:
            return None
        run = WorkflowRun(
            run_id=f"wfrun_{uuid.uuid4().hex[:10]}",
            def_id=def_id, def_version=d.version,
            status="pending", state=dict(initial_state or {}),
            trigger=dict(trigger or {"type": "manual"}),
            step_runs=[StepRun(step_id=s.id) for s in d.steps],
        )
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    def get_run(self, project_id: str, run_id: str) -> WorkflowRun | None:
        raw = self.storage.load_workflow_run(project_id, run_id)
        return WorkflowRun.from_dict(raw) if raw else None

    def list_runs(self, project_id: str,
                  def_id: str | None = None) -> list[WorkflowRun]:
        runs = [WorkflowRun.from_dict(r)
                for r in self.storage.list_workflow_runs(project_id)]
        if def_id is None:
            return runs
        return [r for r in runs if r.def_id == def_id]

    def cancel_run(self, project_id: str, run_id: str) -> WorkflowRun | None:
        run = self.get_run(project_id, run_id)
        if run is None:
            return None
        if run.status in ("completed", "failed", "cancelled"):
            return run
        run.status = "cancelled"
        run.completed_at = _iso_now()
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    # ── Execution ─────────────────────────────────────────────────────────
    def drive_run(self, project_id: str, run_id: str,
                  dispatch_fn: Callable[[str, WorkflowRun], str],
                  *,
                  max_steps: int = 1000) -> WorkflowRun:
        """Drive a run forward, executing action steps synchronously.

        Parking steps (checkpoint / webhook_wait / email_wait) flip
        the run to ``paused`` and return; an external event resolver
        (added in W2-W4) calls back to unpause.

        ``dispatch_fn(step_prompt, run)`` runs the prompt and returns
        the agent's response. State is persisted after each step.
        """
        run = self.get_run(project_id, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        if run.status in ("completed", "failed", "cancelled"):
            return run
        d = self.get_definition(project_id, run.def_id)
        if d is None:
            raise KeyError(f"definition {run.def_id} not found")

        run.status = "running"
        run.started_at = run.started_at or _iso_now()
        self.storage.save_workflow_run(project_id, run.to_dict())

        for idx in range(run.current_step_idx, min(len(d.steps), max_steps)):
            step = d.steps[idx]
            sr = next((s for s in run.step_runs if s.step_id == step.id),
                      StepRun(step_id=step.id))
            if sr.status in ("completed", "skipped"):
                continue

            # Parking steps: leave the run paused; external resolver
            # will call drive_run again after the gate is satisfied.
            if step.type in ("checkpoint", "webhook_wait", "email_wait"):
                run.status = "paused"
                run.current_step_idx = idx
                sr.status = "paused"
                sr.started_at = _iso_now()
                self.storage.save_workflow_run(project_id, run.to_dict())
                return run

            # Validate (W5): for now, action only.
            sr.status = "running"
            sr.started_at = _iso_now()
            self.storage.save_workflow_run(project_id, run.to_dict())

            try:
                # Substitute {placeholder} from state.
                prompt = self._substitute(step.prompt, run.state)
                result = dispatch_fn(prompt, run)
                sr.output = result
                sr.status = "completed"
                sr.completed_at = _iso_now()
                run.state[step.id] = result
            except Exception as e:
                sr.error = f"{type(e).__name__}: {e}"
                sr.status = "failed"
                sr.completed_at = _iso_now()
                run.status = "failed"
                run.completed_at = _iso_now()
                run.current_step_idx = idx
                self.storage.save_workflow_run(project_id, run.to_dict())
                return run

            run.current_step_idx = idx + 1
            self.storage.save_workflow_run(project_id, run.to_dict())

        run.status = "completed"
        run.completed_at = _iso_now()
        run.current_step_idx = len(d.steps)
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    @staticmethod
    def _substitute(prompt: str, scope: dict) -> str:
        import re
        def repl(m):
            key = m.group(1)
            v = scope.get(key)
            return str(v) if v is not None else m.group(0)
        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, prompt)


__all__ = [
    "StepDef", "TriggerDef",
    "WorkflowDefinition", "WorkflowRun", "StepRun",
    "WorkflowService",
]
