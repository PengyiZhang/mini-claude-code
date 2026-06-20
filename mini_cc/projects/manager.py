"""ProjectManager — create/get/list/delete projects under a root.

Each Project bundles a Sandbox, a Storage, and metadata. Multiple projects
can run concurrently in the same process; per-project locking is handled
at the session layer.
"""
from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..core.hooks import Hooks
from ..core.loop import ProjectRef
from ..mcp import MCPPool
from ..sandbox import Policy, SubprocessSandbox
from ..scheduler import CronScheduler
from ..skills import SkillLoader
from ..storage import FSStorage, Storage
from ..teams import TeammateSpawner
from ..tools.background import BackgroundScheduler
from .layout import (ProjectMeta, list_project_ids, read_meta, state_path,
                     write_meta, workspace_path)


StorageFactory = Callable[[Path], Storage]

# Safe ID characters: letters, digits, underscore, hyphen. Used for both
# project_id (when caller-supplied) and any future URL path component.
# Rejects path separators, dots, whitespace — closes the path-traversal
# hole in delete (shutil.rmtree at line 113).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_project_id(project_id: str) -> None:
    if not project_id:
        raise ValueError("project_id must be non-empty")
    if not _SAFE_ID.match(project_id):
        raise ValueError(
            "project_id must match [A-Za-z0-9_-]+ "
            f"(got: {project_id!r})")


def _fs_factory(root: Path) -> Storage:
    return FSStorage(root)


@dataclass
class Project:
    project_id: str
    root: Path                  # the manager root containing all projects
    workspace: Path
    meta: ProjectMeta
    sandbox: SubprocessSandbox
    storage: Storage
    skills_loader: SkillLoader
    scheduler: CronScheduler
    mcp_pool: MCPPool
    background: BackgroundScheduler
    teams: TeammateSpawner
    hooks: Hooks

    def as_ref(self) -> ProjectRef:
        return ProjectRef(
            project_id=self.project_id,
            project_root=str(self.workspace),
            sandbox=self.sandbox,
            storage=self.storage,
            skills_catalog=self.skills_loader.catalog(),
            skills_loader=self.skills_loader,
            scheduler=self.scheduler,
            mcp_pool=self.mcp_pool,
            background=self.background,
            teams=self.teams,
            mcp_servers=self.mcp_pool.list_connected(),
        )

    def rescan_skills(self) -> None:
        """Re-scan skills directory; call after adding/removing skill files."""
        self.skills_loader.scan()


class ProjectManager:
    def __init__(self, root: Path,
                 storage_factory: StorageFactory | None = None,
                 policy: Policy | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_factory = storage_factory or _fs_factory
        self.policy = policy or Policy()

    def _state_root(self) -> Path:
        # All projects share one Storage root for simplicity; FSStorage
        # isolates per-project_id under that root.
        return self.root / ".storage"

    def create(self, tenant_id: str, project_id: str | None = None,
               display_name: str = "") -> Project:
        project_id = project_id or f"proj_{uuid.uuid4().hex[:12]}"
        _validate_project_id(project_id)
        if read_meta(self.root, project_id) is not None:
            raise ValueError(f"project_id already exists: {project_id}")
        meta = ProjectMeta(
            project_id=project_id,
            tenant_id=tenant_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            display_name=display_name or project_id,
        )
        ws = workspace_path(self.root, project_id)
        ws.mkdir(parents=True, exist_ok=True)
        write_meta(self.root, meta)
        return self._assemble(project_id, meta)

    def get(self, project_id: str) -> Project:
        meta = read_meta(self.root, project_id)
        if meta is None:
            raise KeyError(f"project not found: {project_id}")
        return self._assemble(project_id, meta)

    def list(self, tenant_id: str | None = None) -> list[Project]:
        return [self.get(pid) for pid in list_project_ids(self.root, tenant_id)]

    def delete(self, project_id: str) -> None:
        if read_meta(self.root, project_id) is None:
            raise KeyError(f"project not found: {project_id}")
        shutil.rmtree(project_dir_safe(self.root, project_id), ignore_errors=True)

    def _assemble(self, project_id: str, meta: ProjectMeta) -> Project:
        ws = workspace_path(self.root, project_id)
        sandbox = SubprocessSandbox(project_id, ws, policy=self.policy)
        storage = self.storage_factory(self._state_root())
        skills_loader = SkillLoader(ws)
        scheduler = CronScheduler(project_id, storage)
        mcp_pool = MCPPool(project_id)
        background = BackgroundScheduler()
        hooks = Hooks()
        project = Project(
            project_id=project_id,
            root=self.root,
            workspace=ws,
            meta=meta,
            sandbox=sandbox,
            storage=storage,
            skills_loader=skills_loader,
            scheduler=scheduler,
            mcp_pool=mcp_pool,
            background=background,
            teams=None,  # filled in below
            hooks=hooks,
        )
        # TeammateSpawner needs a loop_factory that closes over the Project
        # (and hence its as_ref()), so it has to be built after construction.
        project.teams = TeammateSpawner(
            workspace=ws,
            loop_factory=lambda sid, _p=project: _build_teammate_loop(_p, sid),
            project_id=project_id,
            storage=storage,
        )
        return project


def _build_teammate_loop(project: "Project", session_id: str):
    """Build a sub-AgentLoop for a teammate thread.

    The teammate gets its own SubprocessSandbox so the teams subsystem
    can redirect it (via AgentLoop.set_worktree) into a claimed task's
    worktree without affecting the lead or other teammates.
    """
    from ..core.loop import AgentLoop
    ref = project.as_ref()
    ref.sandbox = SubprocessSandbox(project.project_id, project.workspace)
    return AgentLoop(ref, session_id)


def project_dir_safe(root: Path, project_id: str) -> Path:
    return Path(root) / project_id
