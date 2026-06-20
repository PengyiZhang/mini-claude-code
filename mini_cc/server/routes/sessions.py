"""Session routes: start / list / remove / send (SSE)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse

from ..deps import get_pm, get_sm, require_tenant, validate_id
from ..errors import Conflict, NotFound, map_sdk_exception
from ..schemas import CreateSessionRequest, SendMessageRequest, SessionOut
from ..sse import sse_stream

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions",
    tags=["sessions"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    """Ensure the project exists and belongs to this tenant. Raises 404
    for missing or cross-tenant access."""
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


@router.post("", status_code=201, response_model=SessionOut)
def start_session(body: CreateSessionRequest,
                  pid: str = Path(...),
                  tid: str = Depends(require_tenant),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> SessionOut:
    validate_id(pid)
    if body.session_id is not None:
        validate_id(body.session_id)
    _check_project_tenant(pid, tid, pm)
    try:
        sess = sm.start_session(pid, body.session_id, model=body.model)
    except Exception as e:
        raise map_sdk_exception(e)
    return SessionOut(project_id=pid, session_id=sess.session_id)


@router.get("", response_model=list[str])
def list_sessions(pid: str = Path(...),
                  tid: str = Depends(require_tenant),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> list[str]:
    validate_id(pid)
    _check_project_tenant(pid, tid, pm)
    return sm.list(pid)


@router.delete("/{sid}", status_code=204)
def remove_session(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_tenant),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> None:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    if not sm.remove(pid, sid):
        raise NotFound(f"session {sid} not found")


@router.post("/{sid}/send")
def send_message(body: SendMessageRequest,
                 sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(require_tenant),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> StreamingResponse:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)

    # 404 if the session doesn't exist
    try:
        sess = sm.get(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")

    # 409 if another send is currently holding the project lock.
    if not sm.try_lock(pid):
        raise Conflict(f"project {pid} is busy",
                       details={"code": "project_busy"})

    def sync_iter():
        try:
            yield from sm.send(pid, sid, body.user_input)
        except Exception as e:
            yield {"type": "error",
                   "message": f"{type(e).__name__}: {e}"}

    def _on_cancel():
        # Client disconnect: tell the loop to stop at the next iteration.
        try:
            sess.stop()
        except Exception:
            pass

    return StreamingResponse(
        sse_stream(sync_iter(), on_cancel=_on_cancel),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
