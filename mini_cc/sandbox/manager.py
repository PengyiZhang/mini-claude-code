"""Per-tenant container lifecycle. One TenantContainerManager per tenant id.

Owns:
- Passing the tid (the logical identity) to the runtime. Each runtime
  decides how to use it — DockerRuntime synthesizes ``mini_cc-<sanitized>``
  for the docker CLI; OpenSandboxRuntime indexes by metadata.
- Translating ContainerConfig → mount specs for ensure_running.
- Forwarding exec() calls with workdir=/workspaces/<project_id>.
- ``container_name`` attribute (display-only — kept for CLI/logs).

Does NOT own:
- Env filtering (caller's job; the Sandbox layer applies the policy).
- Image building (caller must ensure the image exists — manager only
  names it).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ContainerConfig
from .runtime import ContainerRuntime, _docker_name_from_tid

# Backwards-compat alias. Older code imported ``_container_name`` from
# this module; it's now ``_docker_name_from_tid`` in runtime.py.
_container_name = _docker_name_from_tid


class TenantContainerManager:
    def __init__(self, tid: str, host_projects_dir: Path,
                 config: ContainerConfig, runtime: ContainerRuntime):
        self.tid = tid
        self.host_projects_dir = host_projects_dir
        self.config = config
        self.runtime = runtime
        # Display-only: the docker-style name when the runtime is
        # DockerRuntime. Other runtimes (OpenSandbox) don't use it.
        self.container_name = _docker_name_from_tid(tid)

    def _mounts(self) -> list[tuple[str, str, str]]:
        """The tenant projects dir is always mounted at /workspaces; any
        extra_mounts from config are appended."""
        mounts = [(str(self.host_projects_dir), "/workspaces", "")]
        for m in self.config.extra_mounts:
            mounts.append((m.host, m.container, m.options))
        return mounts

    def ensure_running(self) -> None:
        self.runtime.ensure_running(
            name=self.tid,
            image=self.config.image_tag,
            mounts=self._mounts(),
            network=self.config.network,
            cpu_quota=self.config.cpu_quota,
            memory_limit=self.config.memory_limit)

    def exec(self, *, project_id: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        self.ensure_running()
        return self.runtime.exec(
            name=self.tid,
            workdir=f"/workspaces/{project_id}",
            command=command,
            timeout=timeout,
            env=env)

    def stop(self) -> None:
        self.runtime.stop(self.tid)

    def remove(self) -> None:
        self.runtime.remove(self.tid)
