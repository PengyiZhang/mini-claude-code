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

from ...workflow.workflow_v2 import WorkflowService
from ..deps import (check_rate_limit_scope, get_pm, get_sm,
                    require_scope, validate_id)
from ..errors import BadRequest, NotFound, Unauthorized


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
    next: str | None = None


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


class WebhookWaitIn(BaseModel):
    """Body for inbound webhook resolution. Free-form — operators
    define their own payload shape; ``event`` is reserved for
    optional event_filter matching at the service layer."""
    event: str | None = None
    data: dict = Field(default_factory=dict)


class EmailWaitIn(BaseModel):
    """Body for inbound email resolution (W4). Accepts either
    from_addr/from (and to_addr/to) — both forms common in real
    inbound-parse webhooks."""
    from_addr: str | None = None
    from_field: str | None = Field(default=None, alias="from")
    to_addr: str | None = None
    to_field: str | None = Field(default=None, alias="to")
    subject: str = ""
    body: str = ""
    raw_headers: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class DriveRunIn(BaseModel):
    """Body for the drive endpoint (W6). Drives the run forward by
    dispatching each action step through the bound session's AgentLoop.
    Returns the final run state — completion, parking, or failure."""
    session_id: str


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


# debug.7.md Task 3b — candidate-approvers picker for checkpoint UI.
# Surfaces the project's spawner roster (alive + recently stopped) so
# the dropdown can pre-populate teammate names instead of forcing the
# user to type blind. Synthetic 'lead' + 'human' entries round out
# the list for self-approve / arbitrary-name cases.
@runs_router.get("/{run_id}/steps/{step_id}/approvers")
def list_approvers(run_id: str = Path(...),
                   step_id: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:read")),
                   pm=Depends(get_pm)) -> list[dict]:
    validate_id(pid)
    # _service_for enforces the tenant boundary; the project it
    # returns carries the .teams spawner we need.
    svc = _service_for(pm, pid, tid)
    project = pm.get(pid)
    from ...workflow.approvers import list_candidate_approvers
    return list_candidate_approvers(project)


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


# W6 — drive a run forward synchronously through a session's AgentLoop.
# Returns the final run state (completed / paused / failed). The UI
# calls this after start_run; the response tells it whether to show
# Approve/Reject buttons (paused at a checkpoint) or the next-step
# preview.
@runs_router.post("/{run_id}/drive", response_model=RunOut)
def drive_run(run_id: str = Path(...),
               body: DriveRunIn = ...,
               pid: str = Path(...),
               tid: str = Depends(check_rate_limit_scope("projects:write")),
               pm=Depends(get_pm),
               sm=Depends(get_sm)) -> RunOut:
    validate_id(pid)
    svc = _service_for(pm, pid, tid)
    # Resolve the bound session's AgentLoop — the workflow's action
    # steps dispatch through it. We warm the session on demand so the
    # caller doesn't need a separate /sessions POST first.
    try:
        sess = sm._ensure_warm(pid, body.session_id)
    except KeyError:
        raise NotFound(f"session {body.session_id} not found")
    loop = sess.loop

    def dispatch(prompt: str, r) -> str:
        # Drive the prompt through one full turn. AgentLoop.run() yields
        # events; we accumulate the assistant's text response. Tool-use
        # activity lands in the transcript and is visible from the chat
        # pane; the workflow only needs the final text.
        chunks: list[str] = []
        try:
            for ev in loop.run(prompt):
                if ev.get("type") == "text":
                    chunks.append(ev.get("text", ""))
                elif ev.get("type") == "error":
                    raise RuntimeError(ev.get("message", "agent error"))
        except Exception:
            raise
        return "".join(chunks).strip()

    try:
        run = svc.drive_run(pid, run_id, dispatch)
    except KeyError:
        raise NotFound(f"run {run_id} not found")
    return _run_to_out(run)


# W3 — inbound webhook for a parked webhook_wait step. External systems
# (GitHub, Stripe, etc.) won't carry a tenant bearer, so this endpoint
# accepts EITHER:
#   - tenant bearer + scope (for in-house callers / tests), OR
#   - a webhook_id query param matching step.config.webhook_id
# The two paths are mutually — whichever matches first wins.
@runs_router.post("/{run_id}/webhook/{step_id}", response_model=RunOut)
def resolve_webhook_wait(run_id: str = Path(...),
                          step_id: str = Path(...),
                          body: WebhookWaitIn | None = None,
                          webhook_id: str | None = None,
                          pid: str = Path(...),
                          pm=Depends(get_pm)) -> RunOut:
    """Inbound webhook for a parked webhook_wait step.

    Auth: when the step configures ``webhook_id``, the caller must
    pass the matching value as the ``webhook_id`` query param. This
    is the shared-secret that replaces the standard tenant bearer
    for external systems (GitHub, Stripe, etc.) that can't carry one.
    """
    validate_id(pid)
    svc = _service_for_unauth(pm, pid)
    # Shared-secret check: when step config has webhook_id, the
    # caller must pass the matching value as a query param.
    run = svc.get_run(pid, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    d = svc.get_definition(pid, run.def_id)
    if d is None:
        raise NotFound(f"definition for run {run_id} not found")
    step = next((s for s in d.steps if s.id == step_id), None)
    if step is None or step.type != "webhook_wait":
        raise NotFound(f"step {step_id} is not a webhook_wait step")
    expected_wh = step.config.get("webhook_id")
    if expected_wh:
        if not webhook_id or webhook_id != expected_wh:
            raise Unauthorized("invalid or missing webhook_id")
    payload = body.model_dump() if body else {}
    try:
        run = svc.resolve_webhook_wait(pid, run_id, step_id, payload)
    except ValueError as e:
        raise BadRequest(str(e))
    return _run_to_out(run)


def _service_for_unauth(pm, pid: str) -> WorkflowService:
    """Same as _service_for but without tenant check — used for the
    inbound webhook path where external callers don't carry a tenant
    bearer. The shared-secret (webhook_id query param) gate replaces
    the tenant boundary for this specific endpoint."""
    try:
        project = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    svc = getattr(project, "workflows_v2", None)
    if svc is None:
        storage = getattr(project, "storage", None)
        if storage is None:
            raise NotFound("workflow storage unavailable")
        svc = WorkflowService(storage)
        try:
            setattr(project, "workflows_v2", svc)
        except Exception:
            object.__setattr__(project, "workflows_v2", svc)
    return svc


# W4 — inbound email for a parked email_wait step. Operators wire an
# inbound email router (SendGrid Inbound Parse, Postmark, procmail →
# curl, etc.) to POST here; the body is the parsed email. The route
# has no tenant bearer gate so external routers can hit it — the
# matching step_id + filters in step.config provide the gate.
@runs_router.post("/{run_id}/email/{step_id}", response_model=RunOut)
def resolve_email_wait(run_id: str = Path(...),
                        step_id: str = Path(...),
                        body: EmailWaitIn | None = None,
                        pid: str = Path(...),
                        pm=Depends(get_pm)) -> RunOut:
    validate_id(pid)
    svc = _service_for_unauth(pm, pid)
    # Build the email dict, honoring both from_addr and from aliases.
    if body is None:
        email = {}
    else:
        d = body.model_dump(by_alias=False)
        email = {
            "from_addr": d.get("from_addr") or d.get("from_field"),
            "to_addr": d.get("to_addr") or d.get("to_field"),
            "subject": d.get("subject") or "",
            "body": d.get("body") or "",
            "raw_headers": d.get("raw_headers") or {},
        }
    try:
        run = svc.resolve_email_wait(pid, run_id, step_id, email)
    except ValueError as e:
        raise BadRequest(str(e))
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return _run_to_out(run)


ALL_ROUTERS = [definitions_router, runs_router]
