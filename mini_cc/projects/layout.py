"""Project directory layout.

    <root>/
        <project_id>/
            workspace/      ← sandbox project_root
            .state/         ← FSStorage root for this project
            meta.json       ← ProjectMeta
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


def project_dir(root: Path, project_id: str) -> Path:
    return Path(root) / project_id


def workspace_path(root: Path, project_id: str) -> Path:
    return project_dir(root, project_id) / "workspace"


def state_path(root: Path, project_id: str) -> Path:
    return project_dir(root, project_id) / ".state"


def meta_path(root: Path, project_id: str) -> Path:
    return project_dir(root, project_id) / "meta.json"


def write_meta(root: Path, meta: ProjectMeta) -> None:
    fp = meta_path(root, meta.project_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2),
                  encoding="utf-8")


def read_meta(root: Path, project_id: str) -> ProjectMeta | None:
    fp = meta_path(root, project_id)
    if not fp.exists():
        return None
    try:
        return ProjectMeta(**json.loads(fp.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def list_project_ids(root: Path, tenant_id: str | None = None) -> list[str]:
    root = Path(root)
    if not root.exists():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = read_meta(root, d.name)
        if meta is None:
            continue
        if tenant_id and meta.tenant_id != tenant_id:
            continue
        out.append(d.name)
    return out
