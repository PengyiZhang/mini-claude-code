"""Permission routes: list pending + decide.

These routes operate on the per-session PermissionInterceptor held by
the AgentLoop. If the project has no `<workspace>/.mini_cc/permissions.toml`,
the interceptor is None — GET returns [] and POST returns 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from ..deps import get_pm, get_sm, require_tenant, validate_id
from ..errors import Conflict, NotFound
from ..schemas import DecidePermissionRequest, PermissionRequestOut

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions",
    tags=["permissions"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


def _get_interceptor(pid: str, sid: str, tid: str, pm, sm):
    """Return (interceptor_or_None, error_or_None). Validates project
    + tenant + session existence along the way."""
    _check_project_tenant(pid, tid, pm)
    try:
        sess = sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")
    return getattr(sess.loop.project, "permissions", None)


@router.get("", response_model=list[PermissionRequestOut])
def list_pending(sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(require_tenant),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> list[PermissionRequestOut]:
    validate_id(pid)
    validate_id(sid)
    interceptor = _get_interceptor(pid, sid, tid, pm, sm)
    if interceptor is None:
        return []
    return [PermissionRequestOut(**r.summary()) for r
            in interceptor.list_pending(sid)]


@router.post("/{req_id}/decide", status_code=204)
def decide(req_id: str,
           body: DecidePermissionRequest,
           sid: str = Path(...),
           pid: str = Path(...),
           tid: str = Depends(require_tenant),
           pm=Depends(get_pm),
           sm=Depends(get_sm)) -> None:
    validate_id(pid)
    validate_id(sid)
    interceptor = _get_interceptor(pid, sid, tid, pm, sm)
    if interceptor is None:
        raise NotFound(f"session {sid} has no permission interceptor "
                       f"(no permissions.toml configured)")
    try:
        ok = interceptor.decide(req_id, body.decision,
                                message=body.message or "")
    except ValueError as e:
        raise Conflict(str(e))
    if not ok:
        raise NotFound(f"permission request {req_id} not found or already decided")
