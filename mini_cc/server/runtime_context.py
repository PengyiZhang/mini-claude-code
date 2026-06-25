"""Server-side wiring for the container sandbox.

Owns the singleton DockerAvailability probe result and the per-tenant
TenantContainerManager cache. Builds the ProjectManager sandbox_factory
closure that:
  1. Calls resolve_kind(tid, tenants_dir) → subprocess | container.
  2. If container: checks docker_available — if False, records a degrade
     event and returns SubprocessSandbox (auto-degrade, user requirement #2).
  3. Otherwise returns ContainerSandbox wired to the tenant's manager.

Backend selection (env ``MINI_CC_SANDBOX_BACKEND``):
  - ``opensandbox`` → OpenSandboxRuntime (HTTP, requires OPEN_SANDBOX_*)
  - ``docker``       → DockerRuntime (subprocess, default)
  - ``auto`` (default) → try opensandbox first (if configured), fall back to docker
At any tier, ``_runtime=None`` means "no container backend" → callers
(SubprocessSandbox) take over. ``docker_available`` is repurposed to mean
"any container backend selected" — the degrade logic stays unchanged.

Tests inject docker_available=False to exercise the degrade path; no
real docker daemon is required because the runtime is also swappable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..auth import TenantKeyRegistry
from ..sandbox import (
    ContainerSandbox, Policy, SubprocessSandbox,
    TenantContainerManager, resolve_kind)
from ..sandbox.runtime import DockerRuntime


@dataclass
class DegradeEvent:
    tenant_id: str
    reason: str


@dataclass
class ServerRuntimeContext:
    data_dir: Path
    key_registry: TenantKeyRegistry
    docker_available: bool
    degrades: list[DegradeEvent] = field(default_factory=list)
    _container_mgrs: dict[str, TenantContainerManager] = field(default_factory=dict)
    _runtime: object = None  # DockerRuntime / OpenSandboxRuntime / test double

    def __post_init__(self):
        if self._runtime is None:
            self._runtime = self._build_runtime()
        # Caller-supplied runtime: trust their docker_available flag.

    def _build_runtime(self):
        """Three-tier fallback: opensandbox → docker → None.

        ``None`` means "no container backend"; ``_sandbox_factory`` will
        auto-degrade to SubprocessSandbox for every container tenant.
        Explicit ``opensandbox`` that fails to init still falls through to
        docker (with a warning) so misconfig doesn't brick the server."""
        backend = os.environ.get("MINI_CC_SANDBOX_BACKEND", "auto")

        if backend in ("opensandbox", "auto"):
            rt = self._try_opensandbox()
            if rt is not None:
                return rt
            if backend == "opensandbox":
                logging.getLogger("mini_cc").warning(
                    "opensandbox backend requested but unavailable; "
                    "falling back to docker")

        # Reachable from any backend tier — docker is the universal fallback.
        from ..sandbox.osdetect import probe_docker
        avail = probe_docker()
        if avail.available:
            return DockerRuntime(prefix=avail.argv_prefix)

        return None

    @staticmethod
    def _try_opensandbox():
        """Return OpenSandboxRuntime if env config + server reachable."""
        # No env config at all → skip the probe entirely (auto mode).
        if not (os.environ.get("OPEN_SANDBOX_DOMAIN")
                or os.environ.get("OPEN_SANDBOX_API_KEY")):
            return None
        try:
            from ..sandbox.opensandbox_runtime import (
                OpenSandboxConfig, OpenSandboxRuntime)
            rt = OpenSandboxRuntime(OpenSandboxConfig.from_env())
            return rt if rt.is_available() else None
        except Exception as exc:
            logging.getLogger("mini_cc").warning(
                "opensandbox backend init failed: %s", exc)
            return None

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    def build_project_manager(self):
        from ..projects import ProjectManager
        return ProjectManager(
            self.data_dir,
            sandbox_factory=self._sandbox_factory,
            data_dir_for_system=self.data_dir)

    def _sandbox_factory(self, tid: str, pid: str, ws: Path,
                         policy: Policy):
        kind, cfg = resolve_kind(tid, self.tenants_dir)
        if kind == "subprocess" or cfg is None:
            return SubprocessSandbox(pid, ws, policy)
        if not self.docker_available:
            self.degrades.append(DegradeEvent(
                tenant_id=tid,
                reason="docker unavailable; falling back to subprocess"))
            return SubprocessSandbox(pid, ws, policy)
        mgr = self._container_mgrs.get(tid)
        if mgr is None:
            host_projects_dir = self.data_dir / "tenants" / tid / "projects"
            mgr = TenantContainerManager(
                tid=tid,
                host_projects_dir=host_projects_dir,
                config=cfg,
                runtime=self._runtime)
            self._container_mgrs[tid] = mgr
        return ContainerSandbox(pid, ws, policy, mgr)

    def shutdown(self) -> None:
        """Stop every managed container. Called from app lifespan shutdown."""
        for mgr in self._container_mgrs.values():
            try:
                mgr.stop()
            except Exception:
                pass
