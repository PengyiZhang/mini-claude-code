"""Admin routes — key management + tenant-scoped metrics.

All routes require an admin-scoped bearer key. Mutations need
``admin:write``; read-only views (key list, metrics dashboard) need
``admin:read``. The key must additionally belong to the ``{tid}`` in
the path — admin keys are tenant-scoped, not superuser.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from ..deps import get_registry, require_scope
from ..errors import BadRequest, Forbidden, NotFound
from ..schemas import (CreateKeyRequest, KeyOut, RotateKeyOut,
                       RotateKeyRequest, UpdateKeyRequest)

router = APIRouter(prefix="/tenants/{tid}/admin", tags=["admin"])


def _to_out(rec) -> KeyOut:
    return KeyOut(
        key=rec.key,
        key_hint=rec.key_hint,
        tenant_id=rec.tenant_id,
        scopes=list(rec.scopes),
        created_at=rec.created_at,
        expires_at=rec.expires_at,
        label=rec.label,
        rotated_from=rec.rotated_from,
    )


def _check_key_belongs_to(rec, tid: str) -> None:
    if rec.tenant_id != tid:
        # Don't leak existence — return 404.
        raise NotFound(f"key not found for tenant {tid}")


@router.get("/keys", response_model=list[KeyOut])
def list_keys(tid: str = Depends(require_scope("admin:read")),
              reg=Depends(get_registry)) -> list[KeyOut]:
    return [_to_out(r) for r in reg.list_for(tid)]


@router.post("/keys", response_model=KeyOut, status_code=201)
def create_key(body: CreateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> KeyOut:
    try:
        rec = reg.generate(
            tid,
            scopes=body.scopes,
            expires_in=body.expires_in,
            label=body.label or "",
        )
    except ValueError as e:
        raise BadRequest(str(e))
    return _to_out(rec)


@router.patch("/keys/{key}", response_model=KeyOut)
def update_key(key: str,
               body: UpdateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> KeyOut:
    # Resolve without lazy-expiry so admins can patch expired keys too.
    rec = reg.find(key)
    if rec is None:
        raise NotFound(f"key not found: {key}")
    _check_key_belongs_to(rec, tid)
    try:
        updated = reg.update(
            key,
            scopes=body.scopes,
            expires_in=body.expires_in,
            label=body.label,
        )
    except ValueError as e:
        raise BadRequest(str(e))
    if updated is None:
        raise NotFound(f"key not found: {key}")
    return _to_out(updated)


@router.delete("/keys/{key}", status_code=204)
def revoke_key(key: str,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> None:
    rec = reg.find(key)
    if rec is None:
        raise NotFound(f"key not found: {key}")
    _check_key_belongs_to(rec, tid)
    if not reg.revoke(key):
        # Race: revoked between lookup and call. Treat as not found.
        raise NotFound(f"key not found: {key}")


@router.post("/keys/{key}/rotate", response_model=RotateKeyOut)
def rotate_key(key: str,
               body: RotateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> RotateKeyOut:
    rec = reg.find(key)
    if rec is None:
        raise NotFound(f"key not found: {key}")
    _check_key_belongs_to(rec, tid)
    try:
        new_rec, old_rec = reg.rotate(
            key,
            grace_hours=body.grace_hours,
            scopes=body.scopes,
            expires_in=body.expires_in,
            label=body.label or "",
        )
    except KeyError:
        raise NotFound(f"key not found: {key}")
    except ValueError as e:
        raise BadRequest(str(e))
    return RotateKeyOut(
        new_key=_to_out(new_rec),
        old_key=_to_out(old_rec) if old_rec is not None else None,
    )


@router.get("/metrics.json")
def tenant_metrics(request: Request,
                   tid: str = Depends(require_scope("admin:read"))) -> dict:
    reg = request.app.state.metrics
    return reg.snapshot_for_tenant(tid)
