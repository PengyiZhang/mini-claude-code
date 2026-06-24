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
from ..core.permissions import PermissionInterceptor
from ..mcp import MCPPool
from ..sandbox import Policy, Sandbox, SubprocessSandbox
from ..scheduler import CronScheduler
from ..skills import SkillLoader
from ..storage import FSStorage, Storage
from ..teams import TeammateSpawner
from ..tools.background import BackgroundScheduler
from .layout import (ProjectMeta, find_metas, find_meta, list_project_ids,
                     read_meta, tenant_storage_dir, write_meta, workspace_path)
from .permissions_config import load_permissions_config


StorageFactory = Callable[[Path], Storage]
SandboxFactory = Callable[[str, str, Path, Policy], Sandbox]

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


def _default_sandbox_factory(tid: str, pid: str, ws: Path,
                             policy: Policy) -> Sandbox:
    """Default: one SubprocessSandbox per project. Server can swap this
    for a container-aware factory at startup (P5)."""
    return SubprocessSandbox(pid, ws, policy)


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
    permissions: PermissionInterceptor | None = None
    prompt_tools: set[str] = None  # type: ignore[assignment]
    _metrics: "object | None" = None  # MetricsRegistry or None
    _sandbox_factory: object = None  # stashed for teammate reuse

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
            permissions=self.permissions,
            prompt_tools=self.prompt_tools or set(),
            tenant_id=self.meta.tenant_id,
            metrics=self._metrics,
        )

    def rescan_skills(self) -> None:
        """Re-scan skills directory; call after adding/removing skill files."""
        self.skills_loader.scan()


class ProjectManager:
    def __init__(self, root: Path,
                 storage_factory: StorageFactory | None = None,
                 policy: Policy | None = None,
                 metrics: "object | None" = None,
                 sandbox_factory: SandboxFactory | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_factory = storage_factory or _fs_factory
        self.policy = policy or Policy()
        self.metrics = metrics
        self._sandbox_factory = sandbox_factory or _default_sandbox_factory

    def _state_root(self, tenant_id: str) -> Path:
        # Per-tenant storage root — two tenants using the same project_id
        # must not share sessions/messages/memory on disk.
        return tenant_storage_dir(self.root, tenant_id)

    def create(self, tenant_id: str, project_id: str | None = None,
               display_name: str = "") -> Project:
        project_id = project_id or f"proj_{uuid.uuid4().hex[:12]}"
        _validate_project_id(project_id)
        if read_meta(self.root, tenant_id, project_id) is not None:
            raise ValueError(f"project_id already exists: {project_id}")
        meta = ProjectMeta(
            project_id=project_id,
            tenant_id=tenant_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            display_name=display_name or project_id,
        )
        ws = workspace_path(self.root, tenant_id, project_id)
        ws.mkdir(parents=True, exist_ok=True)
        write_meta(self.root, meta)
        return self._assemble(project_id, meta)

    def get(self, project_id: str,
            tenant_id: str | None = None) -> Project:
        if tenant_id is not None:
            meta = read_meta(self.root, tenant_id, project_id)
        else:
            meta = find_meta(self.root, project_id)
        if meta is None:
            raise KeyError(f"project not found: {project_id}")
        return self._assemble(project_id, meta)

    def list(self, tenant_id: str | None = None) -> list[Project]:
        return [self.get(pid, tenant_id=tenant_id)
                for pid in list_project_ids(self.root, tenant_id)]

    def delete(self, project_id: str,
               tenant_id: str | None = None) -> None:
        if tenant_id is not None:
            meta = read_meta(self.root, tenant_id, project_id)
            if meta is None:
                raise KeyError(f"project not found: {project_id}")
        else:
            metas = find_metas(self.root, project_id)
            if not metas:
                raise KeyError(f"project not found: {project_id}")
            if len(metas) > 1:
                raise ValueError(
                    f"project_id ambiguous across tenants: {project_id} "
                    f"—— pass tenant_id to disambiguate")
            meta = metas[0]
        # Wipe the on-disk project dir (workspace + meta.json) and the
        # tenant-scoped storage subdir. Both are scoped under
        # <root>/tenants/<tid>/ so the rmtree cannot touch another tenant.
        from .layout import project_dir as _project_dir
        shutil.rmtree(_project_dir(self.root, meta.tenant_id, project_id),
                      ignore_errors=True)
        storage_dir = self._state_root(meta.tenant_id) / project_id
        if storage_dir.exists():
            shutil.rmtree(storage_dir, ignore_errors=True)

    def _assemble(self, project_id: str, meta: ProjectMeta) -> Project:
        ws = workspace_path(self.root, meta.tenant_id, project_id)
        sandbox = self._sandbox_factory(meta.tenant_id, project_id, ws, self.policy)
        storage = self.storage_factory(self._state_root(meta.tenant_id))
        skills_loader = SkillLoader(ws)
        scheduler = CronScheduler(project_id, storage)
        mcp_pool = MCPPool(project_id)
        background = BackgroundScheduler()
        hooks = Hooks()
        # Per-project interactive-permissions config. None when the file
        # is missing → today's behavior (no prompts).
        perm_cfg = load_permissions_config(ws)
        permissions = (PermissionInterceptor(timeout_seconds=perm_cfg.timeout_seconds)
                       if perm_cfg is not None else None)
        prompt_tools = perm_cfg.prompt_tools if perm_cfg is not None else set()
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
            permissions=permissions,
            prompt_tools=prompt_tools,
            _metrics=self.metrics,
            _sandbox_factory=self._sandbox_factory,
        )
        # TeammateSpawner needs a loop_factory that closes over the Project
        # (and hence its as_ref()), so it has to be built after construction.
        project.teams = TeammateSpawner(
            workspace=ws,
            loop_factory=lambda sid, _p=project: _build_teammate_loop(_p, sid),
            project_id=project_id,
            storage=storage,
        )
        # Auto-launch configured MCP servers. Failures are logged via the
        # /mcp command (the pool keeps a record of attempted connections
        # through list_connected); we don't let one broken server block
        # project assembly.
        _connect_configured_mcp_servers(mcp_pool)
        return project


def _connect_configured_mcp_servers(pool: MCPPool) -> None:
    """Read mcp_servers from default_config() and connect each.

    Best-effort: a server that fails to spawn or handshake is skipped.
    Successful connections show up in ``/mcp`` immediately.
    """
    try:
        from ..config import default_config
        servers = default_config().mcp_servers or {}
    except Exception:
        return
    if not servers:
        return
    for name, spec in servers.items():
        command = spec.get("command") or []
        if not isinstance(command, list) or not command:
            continue
        try:
            pool.connect_stdio(
                name, command,
                env=spec.get("env"),
                cwd=spec.get("cwd"),
            )
        except Exception:
            # connect_stdio returns (False, message) rather than raising
            # for the expected error paths; this guard catches any
            # surprise exceptions without aborting the rest of the list.
            pass


def _build_teammate_loop(project: "Project", session_id: str):
    """Build a sub-AgentLoop for a teammate thread.

    The teammate gets its own sandbox via the same factory the parent
    project used, so container-enabled tenants propagate the container
    sandbox to teammates. The teams subsystem can still redirect it
    (via AgentLoop.set_worktree) into a claimed task's worktree.
    """
    from ..core.loop import AgentLoop
    ref = project.as_ref()
    factory = project._sandbox_factory or _default_sandbox_factory
    ref.sandbox = factory(
        project.meta.tenant_id, project.project_id, project.workspace,
        project.sandbox.policy)
    return AgentLoop(ref, session_id)


def project_dir_safe(root: Path, project_id: str) -> Path:
    """Legacy helper retained for callers that haven't been updated to
    pass tenant_id. Returns the first tenant's project_dir if one exists,
    else falls back to a (single-tenant) path under root.

    Deprecated: prefer ProjectManager.delete(pid, tenant_id=tid)."""
    from .layout import find_meta, project_dir as _project_dir
    meta = find_meta(root, project_id)
    if meta is not None:
        return _project_dir(root, meta.tenant_id, project_id)
    return Path(root) / project_id
