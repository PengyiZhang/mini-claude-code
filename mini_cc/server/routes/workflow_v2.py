"""Workflow V2 HTTP routes — definition CRUD + run lifecycle.

Surfaces the WorkflowService data layer (W1) over REST. Resources:

- ``/tenants/{tid}/projects/{pid}/workflow-definitions``
- ``/tenants/{tid}/projects/{pid}/workflow-definitions/{def_id}/runs``
- ``/tenants/{tid}/projects/{pid}/workflow-runs``

Run *driving* is intentionally NOT exposed here in W1 — the service's
``drive_run`` takes a ``dispatch_fn`` that runs prompts through the
AgentLoop, and wiring that through HTTP needs an SSE / polling
contract (W2 lands the checkpoint gate, W6 lands the UI). For now
runs can be started, inspected, and cancelled; ``drive_run`` is
callable from tests and the upcoming UI websocket.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from ...workflow_v2 import WorkflowService
from ..deps import (check_rate_limit_scope, get_pm, require_scope,
                    validate_id)
from ..errors import BadRequest, NotFound


# ── Project → service ──────────────────────────────────────────────────────

def _service_for(pm, pid: str, tid: str) -> WorkflowService:
    """Resolve the project's WorkflowService.

    P0-1: enforce the tenant boundary exactly like the webhooks
    router — without this check, any tenant with a valid key could
    enumerate another tenant's workflow definitions by guessing pid.
    """
    try:
        project = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if project.meta.tenant_id != tid:
        # Same message as the in-project missing case — no info leak.
        raise NotFound(f"project {pid} not found")
    svc = getattr(project, "workflows_v2", None)
    if svc is None:
        # Assemble lazily for projects assembled before this attribute
        # existed. Backed by the same FSStorage instance as everything
        # else on the Project.
        storage = getattr(project, "storage", None)
        if storage is None:
            raise NotFound("workflow storage unavailable")
        svc = WorkflowService(storage)
        try:
            setattr(project, "workflows_v2", svc)
        except Exception:
            object.__setattr__(project, "workflows_v2", svc)
    return svc


# ── Request / response models ──────────────────────────────────────────────

class StepIn(BaseModel):
    id: str
    type: str = "action"
    prompt: str = ""
    inputs_schema: dict = Field(default_factory=dict)
    outputs_schema: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    condition: str | None = None


class TriggerIn(BaseModel):
    type: str = "manual"
    config: dict = Field(default_factory=dict)


class DefinitionIn(BaseModel):
    name: str
    description: str = ""
    steps: list[StepIn] = Field(default_factory=list)
    triggers: list[TriggerIn] = Field(default_factory=list)
    state_schema: dict = Field(default_factory=dict)


class DefinitionOut(BaseModel):
    def_id: str
    name: str
    description: str
    version: int
    owner: str | None = None
    created_at: str
    updated_at: str
    steps: list[dict]
    triggers: list[dict]
    state_schema: dict


def _def_to_out(d) -> DefinitionOut:
    return DefinitionOut(**d.to_dict())


class UpdateDefinitionIn(BaseModel):
    name: str | None = None
    description: str | None = None
    steps: list[StepIn] | None = None
    triggers: list[TriggerIn] | None = None
    state_schema: dict | None = None


class StartRunIn(BaseModel):
    initial_state: dict = Field(default_factory=dict)
    trigger: dict = Field(default_factory=dict)


class ResolveGateIn(BaseModel):
    approver: str | None = None
    decision: str
    feedback: str | None = None


class StepRunOut(BaseModel):
    step_id: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    output: object = None
    error: str | None = None


class RunOut(BaseModel):
    run_id: str
    def_id: str
    def_version: int
    status: str
    current_step_idx: int
    started_at: str | None = None
    completed_at: str | None = None
    state: dict
    step_runs: list[StepRunOut]
    trigger: dict


def _run_to_out(r) -> RunOut:
    return RunOut(**r.to_dict())


# ── Routers ────────────────────────────────────────────────────────────────

definitions_router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/workflow-definitions",
    tags=["workflows-v2"],
)

runs_router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/workflow-runs",
    tags=["workflows-v2"],
)


@definitions_router.get("", response_model=list[DefinitionOut])
def list_definitions(pid: str = Path(...),
                     tid: str = Depends(require_scope("projects:read")),
                     pm=Depends(get_pm)) -> list[DefinitionOut]:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    return [_def_to_out(d) for d in svc.list_definitions(pid)]


@definitions_router.post("", response_model=DefinitionOut, status_code=201)
def create_definition(body: DefinitionIn,
                      pid: str = Path(...),
                      tid: str = Depends(check_rate_limit_scope("projects:write")),
                      pm=Depends(get_pm)) -> DefinitionOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    payload = body.model_dump()
    payload["steps"] = [s.model_dump() for s in body.steps]
    payload["triggers"] = [t.model_dump() for t in body.triggers]
    d = svc.create_definition(pid, payload)
    return _def_to_out(d)


@definitions_router.get("/{def_id}", response_model=DefinitionOut)
def get_definition(def_id: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:read")),
                   pm=Depends(get_pm)) -> DefinitionOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    d = svc.get_definition(pid, def_id)
    if d is None:
        raise NotFound(f"definition {def_id} not found")
    return _def_to_out(d)


@definitions_router.put("/{def_id}", response_model=DefinitionOut)
def update_definition(def_id: str = Path(...),
                      body: UpdateDefinitionIn = ...,
                      pid: str = Path(...),
                      tid: str = Depends(check_rate_limit_scope("projects:write")),
                      pm=Depends(get_pm)) -> DefinitionOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    payload: dict = {}
    if body.name is not None:
        payload["name"] = body.name
    if body.description is not None:
        payload["description"] = body.description
    if body.steps is not None:
        payload["steps"] = [s.model_dump() for s in body.steps]
    if body.triggers is not None:
        payload["triggers"] = [t.model_dump() for t in body.triggers]
    if body.state_schema is not None:
        payload["state_schema"] = body.state_schema
    if not payload:
        raise BadRequest("no fields to update")
    d = svc.update_definition(pid, def_id, payload)
    if d is None:
        raise NotFound(f"definition {def_id} not found")
    return _def_to_out(d)


@definitions_router.delete("/{def_id}", status_code=204)
def delete_definition(def_id: str = Path(...),
                      pid: str = Path(...),
                      tid: str = Depends(check_rate_limit_scope("projects:write")),
                      pm=Depends(get_pm)) -> None:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    if not svc.delete_definition(pid, def_id):
        raise NotFound(f"definition {def_id} not found")


# Runs — start under a definition, then read/cancel from the global
# runs collection. The split mirrors how an operator actually navigates
# ("show me this run" doesn't require knowing its def_id).


@definitions_router.post("/{def_id}/runs",
                          response_model=RunOut, status_code=201)
def start_run(def_id: str = Path(...),
              body: StartRunIn | None = None,
              pid: str = Path(...),
              tid: str = Depends(check_rate_limit_scope("projects:write")),
              pm=Depends(get_pm)) -> RunOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    initial_state = dict(body.initial_state) if body else {}
    trigger = dict(body.trigger) if body and body.trigger else {}
    run = svc.start_run(pid, def_id,
                        initial_state=initial_state, trigger=trigger)
    if run is None:
        raise NotFound(f"definition {def_id} not found")
    return _run_to_out(run)


@runs_router.get("", response_model=list[RunOut])
def list_runs(pid: str = Path(...),
              def_id: str | None = None,
              tid: str = Depends(require_scope("projects:read")),
              pm=Depends(get_pm)) -> list[RunOut]:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    return [_run_to_out(r) for r in svc.list_runs(pid, def_id=def_id)]


@runs_router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str = Path(...),
            pid: str = Path(...),
            tid: str = Depends(require_scope("projects:read")),
            pm=Depends(get_pm)) -> RunOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    run = svc.get_run(pid, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return _run_to_out(run)


@runs_router.delete("/{run_id}", response_model=RunOut)
def cancel_run(run_id: str = Path(...),
               pid: str = Path(...),
               tid: str = Depends(check_rate_limit_scope("projects:write")),
               pm=Depends(get_pm)) -> RunOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    run = svc.cancel_run(pid, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return _run_to_out(run)


# W2 — gate resolution (checkpoint / webhook_wait / email_wait).
# Mounted on the runs router so the approver only needs the run_id
# and step_id to advance a parked gate.
@runs_router.post("/{run_id}/steps/{step_id}/resolve",
                   response_model=RunOut)
def resolve_gate(run_id: str = Path(...),
                 step_id: str = Path(...),
                 body: ResolveGateIn = ...,
                 pid: str = Path(...),
                 tid: str = Depends(check_rate_limit_scope("projects:write")),
                 pm=Depends(get_pm)) -> RunOut:
    validate_id(pid)
    if body.decision not in ("approve", "reject"):
        raise BadRequest("decision must be approve|reject")
    svc = _service_for(pm, pid, tid)
    try:
        run = svc.resolve_gate(pid, run_id, step_id,
                                decision=body.decision,
                                approver=body.approver,
                                feedback=body.feedback)
    except ValueError as e:
        raise BadRequest(str(e))
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return _run_to_out(run)


ALL_ROUTERS = [definitions_router, runs_router]
