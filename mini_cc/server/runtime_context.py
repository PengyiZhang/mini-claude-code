"""Server-side wiring for the container sandbox.

Owns the singleton DockerAvailability probe result and the per-tenant
TenantContainerManager cache. Builds the ProjectManager sandbox_factory
closure that:
  1. Calls resolve_kind(tid, tenants_dir) → subprocess | container.
  2. If container: checks docker_available — if False, records a degrade
     event and returns SubprocessSandbox (auto-degrade, user requirement #2).
  3. Otherwise returns ContainerSandbox wired to the tenant's manager.

Tests inject docker_available=False to exercise the degrade path; no
real docker daemon is required because the runtime is also swappable.
"""
from __future__ import annotations

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
    _runtime: object = None  # DockerRuntime or test double

    def __post_init__(self):
        if self._runtime is None:
            self._runtime = DockerRuntime()
        else:
            # Caller (CLI or test) supplied a runtime; honor docker_available
            # as the source of truth for the degrade decision so tests can
            # simulate "docker missing" without unhooking the runtime.
            pass

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    def build_project_manager(self):
        from ..projects import ProjectManager
        return ProjectManager(
            self.data_dir / "projects",
            sandbox_factory=self._sandbox_factory)

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
            host_projects_dir = self.data_dir / "projects"
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
