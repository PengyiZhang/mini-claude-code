"""POST/GET /tenants/{tid}/projects/{pid}/sessions/{sid}/assets — image
upload + download for the Web UI composer.

Access model
------------
Assets are **project-scoped**, not session-scoped. The session prefix in
the URL exists only for namespacing / lifecycle (uploads tag the source
session in ``src="web:upload:{sid}"``); any caller with ``sessions:read``
on *any* session of the project may read *any* asset in that project's
``.assets`` store. This is by design — teammates within a project share
images, and the design doc (§4.1) explicitly locates the store at
``{project_dir}/.assets`` rather than per-session. Cross-tenant access
still 404s via ``_check_project_tenant``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile
from fastapi.responses import FileResponse

from ..deps import get_pm, require_scope, validate_id
from ..errors import BadRequest, NotFound
from ...assets import AssetValidationError

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions/{sid}/assets",
    tags=["assets"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    """Ensure the project exists and belongs to this tenant. 404 (not
    403) on cross-tenant access so we don't leak project existence."""
    try:
        p = pm.get(pid, tenant_id=tid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


def _ensure_session(pm, sid: str, request: Request, pid: str) -> None:
    """Raise NotFound if the session isn't in memory or on disk. ``sm.get``
    raises KeyError on cold sessions — wrap so callers get a 404 instead
    of leaking as a 500."""
    sm = request.app.state.sm
    try:
        sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")


@router.post("", status_code=201)
def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    pid: str = Path(...),
    sid: str = Path(...),
    tid: str = Depends(require_scope("sessions:write")),
    pm=Depends(get_pm),
):
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid, tenant_id=tid)
    _ensure_session(pm, sid, request, pid)

    data = file.file.read()
    media_type = file.content_type or "application/octet-stream"
    try:
        aid = project.assets.put(
            data, media_type=media_type,
            src=f"web:upload:{sid}")
    except AssetValidationError as e:
        raise BadRequest(str(e))
    meta = project.assets.get_meta(aid)
    return {"asset_id": aid,
            "media_type": meta["media_type"],
            "bytes": meta["bytes"]}


@router.get("/{aid}")
def get_asset(
    pid: str = Path(...),
    sid: str = Path(...),
    aid: str = Path(...),
    tid: str = Depends(require_scope("sessions:read")),
    pm=Depends(get_pm),
):
    validate_id(pid)
    validate_id(sid)
    validate_id(aid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid, tenant_id=tid)
    path = project.assets.get_path(aid)
    if path is None:
        raise NotFound(f"asset {aid} not found")
    meta = project.assets.get_meta(aid) or {}
    return FileResponse(str(path), media_type=meta.get("media_type",
                                                       "application/octet-stream"))


# ── Public unauthenticated route ──────────────────────────────────────
# Mounted at root (no tenant/project/session prefix) so vision MCP tools
# — typically subprocesses without API credentials — can fetch an image
# by signed URL alone. The token IS the auth: HMAC-signed, short-TTL,
# scoped to a single asset. This is the handoff path for the
# vision-gated image feature (MINI_CC_MODEL_VISION=false).

public_router = APIRouter(tags=["assets"])


@public_router.get("/shared/asset/{token}")
def get_shared_asset(token: str, request: Request, pm=Depends(get_pm)):
    """Fetch an asset by signed URL token. No Authorization header
    required — the token's HMAC signature replaces it.

    Used by MCP vision tools (zai-mcp-server, playwright-mcp) to read
    an image attached to a chat message without needing the user's API
    key. Tokens are minted by the agent loop when hydrating image
    blocks for a vision-incapable model.
    """
    from ..errors import Unauthorized
    from ...sharing.tokens import BadShareToken, verify_asset_token
    try:
        claims = verify_asset_token(token)
    except BadShareToken as e:
        raise Unauthorized(f"invalid asset token: {e}")
    try:
        # The signed token carries project_id only — resolve the owning
        # tenant via the explicit cross-tenant lookup (the token's HMAC
        # is the authorization here, not a tenant-scoped bearer).
        project = pm.get_any(claims.project_id)
    except KeyError as e:
        raise NotFound(str(e) or "project not found")
    path = project.assets.get_path(claims.asset_id)
    if path is None:
        raise NotFound(f"asset {claims.asset_id} not found")
    meta = project.assets.get_meta(claims.asset_id) or {}
    return FileResponse(str(path), media_type=meta.get(
        "media_type", "application/octet-stream"))
