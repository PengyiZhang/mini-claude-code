"""Per-tenant container lifecycle. One TenantContainerManager per tenant id.

Owns:
- The sanitized container name (mini_cc-<sanitized_tid>, ≤63 chars).
- Translating ContainerConfig → mount specs for ensure_running.
- Forwarding exec() calls with workdir=/workspaces/<project_id>.

Does NOT own:
- Env filtering (caller's job; the Sandbox layer applies the policy).
- Image building (caller must ensure the image exists — manager only
  names it).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import ContainerConfig
from .runtime import ContainerRuntime

_PREFIX = "mini_cc-"
_MAX_NAME = 63  # Docker container name limit


def _container_name(tid: str) -> str:
    """Sanitize tid to a valid Docker container name.

    Docker names: [A-Za-z0-9][A-Za-z0-9_.-]*. We replace spaces and
    path separators with _ (defensive — ProjectManager already validates
    tid against [A-Za-z0-9_-]+ so most input is already clean).
    """
    if not tid:
        raise ValueError("tid must be non-empty")
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", tid)
    if not sanitized or sanitized[0] in ".-":
        sanitized = "t_" + sanitized
    name = _PREFIX + sanitized
    if len(name) > _MAX_NAME:
        name = _PREFIX + sanitized[-(_MAX_NAME - len(_PREFIX)):]
    return name


class TenantContainerManager:
    def __init__(self, tid: str, host_projects_dir: Path,
                 config: ContainerConfig, runtime: ContainerRuntime):
        self.tid = tid
        self.host_projects_dir = host_projects_dir
        self.config = config
        self.runtime = runtime
        self.container_name = _container_name(tid)

    def _mounts(self) -> list[tuple[str, str, str]]:
        """The tenant projects dir is always mounted at /workspaces; any
        extra_mounts from config are appended."""
        mounts = [(str(self.host_projects_dir), "/workspaces", "")]
        for m in self.config.extra_mounts:
            mounts.append((m.host, m.container, m.options))
        return mounts

    def ensure_running(self) -> None:
        self.runtime.ensure_running(
            name=self.container_name,
            image=self.config.image_tag,
            mounts=self._mounts(),
            network=self.config.network,
            cpu_quota=self.config.cpu_quota,
            memory_limit=self.config.memory_limit)

    def exec(self, *, project_id: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        self.ensure_running()
        return self.runtime.exec(
            name=self.container_name,
            workdir=f"/workspaces/{project_id}",
            command=command,
            timeout=timeout,
            env=env)

    def stop(self) -> None:
        self.runtime.stop(self.container_name)

    def remove(self) -> None:
        self.runtime.remove(self.container_name)
