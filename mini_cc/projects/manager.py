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

from ..channels import ChannelRegistry
from ..core.hooks import Hooks
from ..core.loop import ProjectRef
from ..core.permissions import PermissionInterceptor
from ..mcp import MCPPool
from ..sandbox import Policy, Sandbox, SubprocessSandbox
from ..scheduler import CronScheduler
from ..sharing.webhooks import WebhookRegistry
from ..skills import SkillLoader
from ..memory import MemoryLoader
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
    memory_loader: MemoryLoader
    scheduler: CronScheduler
    mcp_pool: MCPPool
    background: BackgroundScheduler
    teams: TeammateSpawner
    hooks: Hooks
    permissions: PermissionInterceptor | None = None
    prompt_tools: set[str] = None  # type: ignore[assignment]
    _metrics: "object | None" = None  # MetricsRegistry or None
    _sandbox_factory: object = None  # stashed for teammate reuse
    # F7.2: project-scoped webhook registry. None when storage has no
    # filesystem root (non-FS backends) — SessionManager then skips
    # dispatcher installation. Cached so the API routes and the live
    # dispatcher share one in-memory object: a webhook added via HTTP
    # is visible to the next event without re-warming the session.
    webhooks: "WebhookRegistry | None" = None
    # Bidirectional channel registry (Feishu / Slack / …). Same pattern
    # as webhooks: cached on Project so the HTTP routes + the live
    # dispatcher share one object. None when storage has no FS root.
    channels: "ChannelRegistry | None" = None

    @property
    def metrics(self):
        """Public alias for ``_metrics`` — exposes MetricsRegistry on the
        Project surface so command handlers (e.g. ``/cost``) and tests
        can read it without reaching past the underscore-private convention.
        ProjectRef already mirrors this as ``ProjectRef.metrics``."""
        return self._metrics

    def as_ref(self) -> ProjectRef:
        return ProjectRef(
            project_id=self.project_id,
            project_root=str(self.workspace),
            sandbox=self.sandbox,
            storage=self.storage,
            skills_catalog=self.skills_loader.catalog(),
            skills_loader=self.skills_loader,
            memory_loader=self.memory_loader,
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
                 sandbox_factory: SandboxFactory | None = None,
                 data_dir_for_system: Path | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_factory = storage_factory or _fs_factory
        self.policy = policy or Policy()
        self.metrics = metrics
        self._sandbox_factory = sandbox_factory or _default_sandbox_factory
        # System-tier plugin dir. Defaults to ``root`` so the simple
        # single-ProjectManager case (root == data_dir, as in tests + SDK
        # direct-use) still discovers <root>/.mini_cc/. Server passes
        # the real data_dir explicitly so the same ProjectManager finds
        # <data_dir>/.mini_cc/ even when root != data_dir.
        self.data_dir = Path(data_dir_for_system or self.root).resolve()
        # Assembled-project cache, keyed by (tenant_id, project_id).
        # Without this, every pm.get() (called by every API route) rebuilt
        # the whole Project + re-ran the MCP auto-connect sweep — ~2s for a
        # remote HTTP server, per request, leaking MCP subprocesses each
        # time. Caching makes the warm path O(microseconds) and guarantees
        # route handlers and the SessionManager's warm loops share ONE
        # Project object. See _config_signature for the invalidation rule.
        self._cache: dict[tuple[str, str], Project] = {}
        self._sigs: dict[tuple[str, str], tuple] = {}

    def _config_signature(self, tenant_id: str, workspace: Path) -> tuple:
        """Cheap fingerprint of the MCP plugin config across all tiers.

        We stat ``.mcp.json``, ``mcp.toml``, and ``permissions.toml`` in
        each tier dir and pack (path, mtime, size) into a tuple. A changed
        signature invalidates the assembled-project cache so a freshly-edited
        config file is picked up on the next ``get()`` without a server
        restart. Stats are ~microseconds, so checking on every (cached)
        get is fine. (Skills have their own hot-rescan via /skills +
        SkillLoader.scan, so they're not part of this fingerprint.)
        """
        from ..plugins import project_tier_dirs
        sig: list = []
        for td in project_tier_dirs(self.data_dir, tenant_id, workspace):
            for name in (".mcp.json", "mcp.toml", "permissions.toml"):
                fp = td / name
                try:
                    st = fp.stat()
                    sig.append((str(fp), st.st_mtime_ns, st.st_size))
                except OSError:
                    sig.append((str(fp), 0, 0))
        return tuple(sig)

    def invalidate(self, project_id: str,
                   tenant_id: str | None = None) -> None:
        """Drop the cached assembled Project (if any). Next ``get()``
        re-assembles from scratch. Call after programmatic config changes
        that bypass the filesystem mtime signal."""
        if tenant_id is None:
            meta = find_meta(self.root, project_id)
            if meta is None:
                return
            tenant_id = meta.tenant_id
        # Stop WS-mode receivers for this project before dropping the
        # cached Project — _assemble will respawn them on next get().
        # Best-effort: never block cache invalidation on supervisor
        # teardown.
        try:
            from ..channels import get_supervisor
            get_supervisor().stop_project(tenant_id, project_id)
        except Exception:
            pass
        self._cache.pop((tenant_id, project_id), None)
        self._sigs.pop((tenant_id, project_id), None)

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
        # Bootstrap plugin tiers: project + tenant. System tier is
        # expected to be set up by the operator (or the server CLI) and
        # is created lazily by discovery; we don't touch it here.
        from ..plugins import ensure_tier_dir, PluginTier
        ensure_tier_dir(self.data_dir, PluginTier.TENANT, tenant_id=tenant_id)
        ws = workspace_path(self.root, tenant_id, project_id)
        ws.mkdir(parents=True, exist_ok=True)
        from ..plugins import ensure_tier_dir as _ensure, PluginTier as _T
        _ensure(ws, _T.PROJECT)
        write_meta(self.root, meta)
        # NOTE: we deliberately do NOT prime the cache here. ``create()``
        # returns before the caller has finished configuring the workspace
        # (e.g. writing .mini_cc/permissions.toml). Priming now would pin
        # a project assembled against a half-configured workspace, and a
        # later get() would return that stale object. The first get()
        # assembles lazily — after the workspace is fully set up — and
        # caches that. create()'s returned project is the same shape, just
        # not the cached identity.
        return self._assemble(project_id, meta)

    def get(self, project_id: str,
            tenant_id: str | None = None) -> Project:
        """Fetch ``project_id`` for ``tenant_id``. ``tenant_id`` is
        REQUIRED (S3): a forgotten tid must never silently widen the
        lookup to every tenant. Cross-tenant tools use ``get_any``."""
        if not tenant_id:
            raise ValueError(
                "tenant_id is required for get(); cross-tenant lookup "
                "must use get_any() explicitly")
        meta = read_meta(self.root, tenant_id, project_id)
        if meta is None:
            raise KeyError(f"project not found: {project_id}")
        key = (meta.tenant_id, project_id)
        ws = workspace_path(self.root, meta.tenant_id, project_id)
        # Cache hit only when the MCP config fingerprint is unchanged —
        # editing .mini_cc/.mcp.json bumps an mtime and forces a rebuild.
        sig = self._config_signature(meta.tenant_id, ws)
        cached = self._cache.get(key)
        if cached is not None and self._sigs.get(key) == sig:
            return cached
        project = self._assemble(project_id, meta)
        self._cache[key] = project
        self._sigs[key] = sig
        return project

    def get_any(self, project_id: str) -> Project:
        """EXPLICIT cross-tenant lookup by global project scan. For
        internal tooling (migrations, channel-index rebuilds) only —
        request paths must resolve tenant first."""
        meta = find_meta(self.root, project_id)
        if meta is None:
            raise KeyError(f"project not found: {project_id}")
        return self.get(project_id, tenant_id=meta.tenant_id)

    def list(self, tenant_id: str | None = None) -> list[Project]:
        if not tenant_id:
            raise ValueError(
                "tenant_id is required for list(); cross-tenant scans "
                "must use list_all() explicitly")
        return [self.get(pid, tenant_id=tenant_id)
                for pid in list_project_ids(self.root, tenant_id)]

    def list_all(self) -> list[Project]:
        """EXPLICIT cross-tenant listing (internal tooling only)."""
        return [self.get_any(pid) for pid in list_project_ids(self.root, None)]

    def delete(self, project_id: str,
               tenant_id: str | None = None) -> None:
        if not tenant_id:
            raise ValueError(
                "tenant_id is required for delete(); destructive calls "
                "must never scan across tenants")
        meta = read_meta(self.root, tenant_id, project_id)
        if meta is None:
            raise KeyError(f"project not found: {project_id}")
        # Wipe the on-disk project dir (workspace + meta.json) and the
        # tenant-scoped storage subdir. Both are scoped under
        # <root>/tenants/<tid>/ so the rmtree cannot touch another tenant.
        from .layout import project_dir as _project_dir
        shutil.rmtree(_project_dir(self.root, meta.tenant_id, project_id),
                      ignore_errors=True)
        storage_dir = self._state_root(meta.tenant_id) / project_id
        if storage_dir.exists():
            shutil.rmtree(storage_dir, ignore_errors=True)
        # Drop any cached handle so it can't be re-served after deletion.
        self._cache.pop((meta.tenant_id, project_id), None)
        self._sigs.pop((meta.tenant_id, project_id), None)

    def _assemble(self, project_id: str, meta: ProjectMeta) -> Project:
        ws = workspace_path(self.root, meta.tenant_id, project_id)
        sandbox = self._sandbox_factory(meta.tenant_id, project_id, ws, self.policy)
        storage = self.storage_factory(self._state_root(meta.tenant_id))
        skills_loader = SkillLoader(
            ws, data_dir=self.data_dir, tenant_id=meta.tenant_id)
        memory_loader = MemoryLoader(
            ws, data_dir=self.data_dir, tenant_id=meta.tenant_id)
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
            memory_loader=memory_loader,
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
        _connect_configured_mcp_servers(
            mcp_pool, data_dir=self.data_dir,
            tenant_id=meta.tenant_id, workspace=ws)
        # F7.2: cache the project's WebhookRegistry on the Project so the
        # HTTP routes and SessionManager's dispatcher share one in-memory
        # object. Skip when storage has no FS root (non-FS backends).
        storage_root = getattr(storage, "root", None)
        if storage_root is not None:
            project.webhooks = WebhookRegistry(storage_root, project_id)
            project.channels = ChannelRegistry(storage_root, project_id)
            # Spawn WS-mode receivers for every ws binding on disk. Same
            # pattern as MCP auto-connect above: failures don't block
            # project assembly, just log a warning. Supervisor is a
            # process-wide singleton (MCPPool-style) so re-assembling
            # the project (e.g. after pm.invalidate) restarts cleanly.
            try:
                from ..channels import get_supervisor
                sup = get_supervisor()
                for b in project.channels.list():
                    if getattr(b, "transport", "webhook") == "ws" and getattr(b, "enable", "false") == "true":
                        sup.start_for(project, b)
            except Exception:
                # Channels subsystem might be unavailable (legacy deploy
                # without lark-oapi). Don't let WS spawn failures break
                # project assembly.
                pass
        # Workflow V2 (W1): same pattern — one WorkflowService per
        # project, backed by the same FSStorage, shared between the
        # HTTP routes and any in-process driver (tests, future UI).
        try:
            from ..workflow.workflow_v2 import WorkflowService
            project.workflows_v2 = WorkflowService(storage)
        except Exception:
            # If the import ever fails (circular dep during a refactor),
            # the HTTP layer lazily assembles one on first request.
            pass
        return project


def _connect_configured_mcp_servers(pool: MCPPool, *,
                                    data_dir: Path | None = None,
                                    tenant_id: str | None = None,
                                    workspace: Path | None = None) -> None:
    """Read mcp_servers from every available source and connect each.

    Sources merged in priority order (later wins on name clash):

    1. ``<data_dir>/.mini_cc/``             — system tier
       (``.mcp.json`` overrides ``mcp.toml`` within a tier)
    2. ``<data_dir>/tenants/<tid>/.mini_cc/`` — tenant tier
    3. ``<workspace>/.mini_cc/``            — project tier
    4. ``default_config().mcp_servers``     — env (legacy escape hatch)

    Each spec is dispatched by ``type`` (stdio/http/sse) via
    :meth:`MCPPool.connect_from_spec`. Best-effort: a server that fails
    to spawn or handshake is recorded on the pool's attempt log (visible
    in ``/mcp`` under "failed to connect") rather than aborting
    assembly. Successful connections show up in ``/mcp`` immediately.
    """
    from ..plugins import (PluginTier, discover_mcp_servers, project_tier_dirs)
    tier_dirs: list[Path] = []
    if data_dir is not None and tenant_id is not None and workspace is not None:
        tier_dirs = project_tier_dirs(data_dir, tenant_id, workspace)
    servers: dict[str, dict] = discover_mcp_servers(tier_dirs)
    # Env / programmatic override last → wins. Legacy shape is the bare
    # ``{command: [...]}``; default to stdio if ``type`` is missing.
    try:
        from ..config import default_config
        env_servers = default_config().mcp_servers or {}
        if env_servers:
            for k, v in env_servers.items():
                if isinstance(v, dict) and "type" not in v:
                    v = {**v, "type": "stdio"}
                servers[k] = v
    except Exception:
        pass
    if not servers:
        return
    for name, spec in servers.items():
        try:
            pool.connect_from_spec(name, spec)
        except Exception:
            # connect_from_spec returns (False, message) for expected
            # error paths; this guard catches surprise exceptions without
            # aborting the rest of the list.
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
