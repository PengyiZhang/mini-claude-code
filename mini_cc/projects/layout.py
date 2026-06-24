"""Tenant-scoped project directory layout.

    <root>/
        tenants/
            <tenant_id>/
                projects/
                    <project_id>/
                        workspace/    ← sandbox project_root
                        .state/       ← FSStorage root for this project
                        meta.json     ← ProjectMeta
                .storage/              ← tenant-scoped storage root

Tenant isolation: two tenants using the same project_id get separate
on-disk workspaces AND separate storage roots, so neither workspace
files nor session/messages/todos leak across tenants.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ProjectMeta:
    project_id: str
    tenant_id: str
    created_at: str
    display_name: str = ""


def _tenant_dir(root: Path, tenant_id: str) -> Path:
    return Path(root) / "tenants" / tenant_id


def tenant_projects_dir(root: Path, tenant_id: str) -> Path:
    return _tenant_dir(root, tenant_id) / "projects"


def tenant_storage_dir(root: Path, tenant_id: str) -> Path:
    return _tenant_dir(root, tenant_id) / ".storage"


def project_dir(root: Path, tenant_id: str, project_id: str) -> Path:
    return tenant_projects_dir(root, tenant_id) / project_id


def workspace_path(root: Path, tenant_id: str, project_id: str) -> Path:
    return project_dir(root, tenant_id, project_id) / "workspace"


def state_path(root: Path, tenant_id: str, project_id: str) -> Path:
    return project_dir(root, tenant_id, project_id) / ".state"


def meta_path(root: Path, tenant_id: str, project_id: str) -> Path:
    return project_dir(root, tenant_id, project_id) / "meta.json"


def write_meta(root: Path, meta: ProjectMeta) -> None:
    fp = meta_path(root, meta.tenant_id, meta.project_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2),
                  encoding="utf-8")


def read_meta(root: Path, tenant_id: str, project_id: str) -> ProjectMeta | None:
    fp = meta_path(root, tenant_id, project_id)
    if not fp.exists():
        return None
    try:
        return ProjectMeta(**json.loads(fp.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def find_meta(root: Path, project_id: str) -> ProjectMeta | None:
    """Locate a project's meta by scanning all tenants. Used by callers
    that only know the project_id (e.g. legacy API paths). When the same
    project_id exists under multiple tenants, returns the first match —
    callers that know the tenant_id should call read_meta() instead."""
    root = Path(root)
    tenants_dir = root / "tenants"
    if not tenants_dir.exists():
        return None
    for tenant_dir in tenants_dir.iterdir():
        if not tenant_dir.is_dir():
            continue
        meta = read_meta(root, tenant_dir.name, project_id)
        if meta is not None:
            return meta
    return None


def find_metas(root: Path, project_id: str) -> list[ProjectMeta]:
    """All metas matching project_id across tenants. Used to detect
    ambiguous deletes."""
    root = Path(root)
    tenants_dir = root / "tenants"
    if not tenants_dir.exists():
        return []
    out = []
    for tenant_dir in tenants_dir.iterdir():
        if not tenant_dir.is_dir():
            continue
        meta = read_meta(root, tenant_dir.name, project_id)
        if meta is not None:
            out.append(meta)
    return out


def list_project_ids(root: Path, tenant_id: str | None = None) -> list[str]:
    root = Path(root)
    if tenant_id is not None:
        return _list_under_tenant(root, tenant_id)
    out: list[str] = []
    tenants_dir = root / "tenants"
    if not tenants_dir.exists():
        return out
    for tenant_dir in tenants_dir.iterdir():
        if not tenant_dir.is_dir():
            continue
        out.extend(_list_under_tenant(root, tenant_dir.name))
    return out


def _list_under_tenant(root: Path, tenant_id: str) -> list[str]:
    projects_dir = tenant_projects_dir(root, tenant_id)
    if not projects_dir.exists():
        return []
    out = []
    for d in projects_dir.iterdir():
        if not d.is_dir():
            continue
        meta = read_meta(root, tenant_id, d.name)
        if meta is None:
            continue
        out.append(d.name)
    return out
