"""Run Table routes — read-only JSON views of the active session's
long-running workflows and background tasks, for the sidebar "Run Table"
panel.

Background tasks and the active workflow are *session-bound* runtime state
(they live on the AgentLoop's ProjectRef instance, which is captured at
session-start time). They are NOT re-derivable from a fresh ``pm.get()`` —
that would assemble a new Project with an empty BackgroundScheduler and no
active_workflow. So these endpoints go through the SessionManager: they
warm the session and read straight off ``loop.project``. Saved workflows
are project-scoped on disk, so those come from storage.

Mutations (save / load / delete a workflow, stop a background task) are
not re-implemented here — they go through the existing slash-command
endpoint (``POST .../commands/{name}``) so we reuse the tested handlers.
The panel refetches this JSON after each action.

Endpoints (session-scoped):

    GET /tenants/{tid}/projects/{pid}/sessions/{sid}/run-table/workflows
        → {"active": <workflow dict | null>, "saved": [<workflow dict>, ...]}
    GET /tenants/{tid}/projects/{pid}/sessions/{sid}/run-table/background
        → [<task dict>, ...]
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from ..deps import get_pm, get_sm, require_scope, validate_id
from ..errors import NotFound

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions",
    tags=["run-table"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


def _loop_or_404(pid: str, sid: str, sm):
    """Warm the session and return its AgentLoop's ProjectRef. 404 if the
    session is neither in memory nor on disk."""
    try:
        sess = sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")
    loop = getattr(sess, "loop", None)
    if loop is None:
        raise NotFound(f"session {sid} has no loop")
    return getattr(loop, "project", None)


def _workflow_to_dict(wf) -> dict:
    """Active workflows are Workflow dataclasses with to_dict(); fall back
    to the plain dict shape for anything already dict-like."""
    to_dict = getattr(wf, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(wf, dict):
        return wf
    return {"name": str(wf)}


@router.get("/{sid}/run-table/workflows")
def get_workflows(sid: str = Path(...),
                  pid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:read")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> dict:
    """Return the session's active workflow plus the project's saved
    workflows."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    ref = _loop_or_404(pid, sid, sm)
    active = getattr(ref, "active_workflow", None)
    storage = getattr(ref, "storage", None)
    saved: list[dict] = []
    if storage is not None and hasattr(storage, "list_workflows"):
        try:
            saved = list(storage.list_workflows(pid))
        except Exception:
            saved = []
    return {
        "active": _workflow_to_dict(active) if active is not None else None,
        "saved": saved,
    }


@router.get("/{sid}/run-table/background")
def get_background(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("sessions:read")),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> list[dict]:
    """Return the session's background-task roster."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    ref = _loop_or_404(pid, sid, sm)
    bg = getattr(ref, "background", None)
    if bg is None or not hasattr(bg, "list_tasks"):
        return []
    try:
        tasks = bg.list_tasks()
    except Exception:
        return []
    return [t if isinstance(t, dict) else dict(t) for t in tasks]
