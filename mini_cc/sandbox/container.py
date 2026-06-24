"""ContainerSandbox — Sandbox protocol impl where execute/git run inside
a per-tenant Docker container and fs ops stay on host.

Fs delegation reuses the path-isolation logic of SubprocessSandbox —
the workspace is bind-mounted into the container at /workspaces/<pid>,
so in-container processes see the same files the host-side fs ops see.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import CommandBlockedError
from .manager import TenantContainerManager
from .policy import Policy
from .subprocess_sandbox import SubprocessSandbox


class ContainerSandbox:
    """Sandbox that delegates execute()/git() to a container.

    The first execute()/git() call triggers ``ensure_running()`` on the
    tenant's container (idempotent thereafter). Fs operations never touch
    the container — they run through the embedded SubprocessSandbox on
    the host, which already enforces project-root path isolation.
    """

    def __init__(self, project_id: str, project_root: Path,
                 policy: Policy, container_mgr: TenantContainerManager):
        self.project_id = project_id
        self.project_root = Path(project_root).resolve()
        self.policy = policy
        self._fs = SubprocessSandbox(project_id, project_root, policy)
        self._mgr = container_mgr

    # ── Fs: pure delegation ────────────────────────────────────────────────
    def resolve_path(self, rel):
        return self._fs.resolve_path(rel)

    def validate_path(self, path):
        return self._fs.validate_path(path)

    def read(self, *a, **kw):
        return self._fs.read(*a, **kw)

    def write(self, *a, **kw):
        return self._fs.write(*a, **kw)

    def edit(self, *a, **kw):
        return self._fs.edit(*a, **kw)

    def glob(self, *a, **kw):
        return self._fs.glob(*a, **kw)

    def grep(self, *a, **kw):
        return self._fs.grep(*a, **kw)

    # ── Process: container ────────────────────────────────────────────────
    def _filtered_env(self, extra: dict | None) -> dict[str, str]:
        """Apply the same env whitelist SubprocessSandbox uses, then set
        HOME to the in-container project dir so tools that look at $HOME
        (git config, npm cache) land inside the workspace."""
        env = {k: v for k, v in os.environ.items()
               if k in self.policy.allowed_env}
        env["HOME"] = f"/workspaces/{self.project_id}"
        env.pop("USERPROFILE", None)  # Windows-only, would mislead Linux tools.
        if extra:
            env.update(extra)
        return env

    def execute(self, command, *, timeout=120, env=None, cwd=None):
        violations = self.policy.scan_command(command)
        if violations:
            raise CommandBlockedError(violations)
        # Validate cwd against project_root (same rule as SubprocessSandbox).
        # We don't pass cwd to docker exec — the container always cds to
        # /workspaces/<pid>. If a relative cwd was requested, prepend a
        # `cd <rel>` so the in-container shell lands there.
        if cwd is not None:
            resolved = self._fs._resolve_run_cwd(cwd)
            rel = resolved.relative_to(self.project_root)
            command = f"cd {rel.as_posix()} && {command}"
        return self._mgr.exec(
            project_id=self.project_id,
            command=command,
            timeout=timeout,
            env=self._filtered_env(env))

    def git(self, args, *, timeout=60):
        v = self.policy.check_git_args(args)
        if v:
            raise CommandBlockedError([v])
        cmd_str = " ".join(["git"] + [str(a) for a in args])
        return self._mgr.exec(
            project_id=self.project_id,
            command=cmd_str,
            timeout=timeout,
            env=self._filtered_env(None))
