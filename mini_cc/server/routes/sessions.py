"""Session routes: start / list / remove / resume / send (SSE)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import StreamingResponse

from ..deps import (check_rate_limit_scope, get_pm, get_sm, require_scope,
                    validate_id)
from ..errors import Conflict, NotFound, map_sdk_exception
from ..schemas import (CreateSessionRequest, SendMessageRequest, SessionMeta,
                       SessionOut)
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


@router.post("", response_model=SessionOut)
def start_session(body: CreateSessionRequest,
                  response: Response,
                  pid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:write")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> SessionOut:
    """Idempotent: 201 if a new session was created, 200 if an existing
    on-disk session was re-warmed."""
    validate_id(pid)
    if body.session_id is not None:
        validate_id(body.session_id)
    _check_project_tenant(pid, tid, pm)

    project = pm.get(pid)
    existed = (body.session_id is not None
               and body.session_id in {m.session_id for m
                                       in project.storage.list_sessions(pid)})

    try:
        sess = sm.start_session(pid, body.session_id, model=body.model)
    except Exception as e:
        raise map_sdk_exception(e)
    response.status_code = 200 if existed else 201
    return SessionOut(project_id=pid, session_id=sess.session_id,
                      created=not existed)


@router.get("", response_model=list[SessionMeta])
def list_sessions(pid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:read")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> list[SessionMeta]:
    validate_id(pid)
    _check_project_tenant(pid, tid, pm)
    return [SessionMeta(**m.__dict__) for m in sm.list(pid)]


@router.post("/{sid}/resume", response_model=SessionMeta)
def resume_session(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("sessions:write")),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> SessionMeta:
    """Explicit warm-load of an on-disk session. 200 if warmed (idempotent
    if already in memory). 404 if not on disk."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    try:
        sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")
    # Re-read the meta so in_memory=True reflects the just-completed warm.
    metas = {m.session_id: m for m in sm.list(pid)}
    m = metas.get(sid)
    if m is None:
        raise NotFound(f"session {sid} not found")
    return SessionMeta(**m.__dict__)


@router.delete("/{sid}", status_code=204)
def remove_session(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("sessions:write")),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> None:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    if not sm.remove(pid, sid):
        raise NotFound(f"session {sid} not found")


@router.get("/{sid}/messages")
def get_messages(sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(require_scope("sessions:read")),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> list[dict]:
    """Return the persisted transcript for hydration after page reload.

    Returns the raw Anthropic-format message list. The frontend converts
    these into its ChatMessage shape (merging text + tool_use blocks into
    a single assistant bubble and pairing tool_use_ids with the tool_result
    that follows in the next user turn).
    """
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)
    if sid not in {m.session_id for m in project.storage.list_sessions(pid)}:
        raise NotFound(f"session {sid} not found")
    msgs = project.storage.load_messages(pid, sid)
    # Drop non-serializable bits (shouldn't happen with JSON-on-disk, but
    # be defensive — pydantic models, datetimes, etc. would blow up json).
    return [_to_jsonable(m) for m in msgs]


def _to_jsonable(obj):
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_jsonable(v) for v in obj]
    return obj


@router.get("/{sid}/todos")
def get_todos(sid: str = Path(...),
              pid: str = Path(...),
              tid: str = Depends(require_scope("sessions:read")),
              pm=Depends(get_pm),
              sm=Depends(get_sm)) -> list[dict]:
    """Return persisted todos for the session — hydrates the task board
    on page reload. Same shape the loop emits via ``todos_updated`` events."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)
    if sid not in {m.session_id for m in project.storage.list_sessions(pid)}:
        raise NotFound(f"session {sid} not found")
    return project.storage.load_todos(pid, sid)


@router.post("/{sid}/send")
def send_message(body: SendMessageRequest,
                 sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(check_rate_limit_scope("sessions:write")),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> StreamingResponse:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)

    # Auto-resume: cold sessions are warmed here. 404 only if neither
    # in-memory nor on-disk.
    try:
        sess = sm._ensure_warm(pid, sid)
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
