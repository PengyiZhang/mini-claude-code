"""Project CRUD routes. All keyed off the tenant_id resolved from the
bearer token (which must match {tid} in the path)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from ..deps import get_pm, require_tenant, validate_id
from ..errors import Conflict, NotFound, map_sdk_exception
from ..schemas import CreateProjectRequest, ProjectOut

router = APIRouter(prefix="/tenants/{tid}/projects", tags=["projects"])


def _to_out(p) -> ProjectOut:
    return ProjectOut(
        project_id=p.project_id,
        tenant_id=p.meta.tenant_id,
        display_name=p.meta.display_name,
        created_at=p.meta.created_at,
    )


@router.post("", status_code=201, response_model=ProjectOut)
def create_project(body: CreateProjectRequest,
                   tid: str = Depends(require_tenant),
                   pm=Depends(get_pm)) -> ProjectOut:
    try:
        if body.project_id is not None:
            validate_id(body.project_id)
        p = pm.create(tenant_id=tid, project_id=body.project_id,
                      display_name=body.display_name or "")
    except Conflict:
        raise
    except Exception as e:
        raise map_sdk_exception(e)
    return _to_out(p)


@router.get("", response_model=list[ProjectOut])
def list_projects(tid: str = Depends(require_tenant),
                  pm=Depends(get_pm)) -> list[ProjectOut]:
    return [_to_out(p) for p in pm.list(tenant_id=tid)]


@router.get("/{pid}", response_model=ProjectOut)
def get_project(pid: str = Path(...),
                tid: str = Depends(require_tenant),
                pm=Depends(get_pm)) -> ProjectOut:
    validate_id(pid)
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        # Tenant-authenticated but trying to read another tenant's project.
        raise NotFound(f"project {pid} not found")
    return _to_out(p)


@router.delete("/{pid}", status_code=204)
def delete_project(pid: str = Path(...),
                   tid: str = Depends(require_tenant),
                   pm=Depends(get_pm)) -> None:
    validate_id(pid)
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")
    try:
        pm.delete(pid)
    except Exception as e:
        raise map_sdk_exception(e)
