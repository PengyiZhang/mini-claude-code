"""ContainerRuntime: abstraction over the docker CLI.

Two implementations:
- ``DockerRuntime`` — real docker subprocess calls.
- ``FakeRuntime`` — records every call into ``.calls`` list; used by tests.

The Protocol is narrow: ensure_running, exec, status, stop, remove,
list_managed, is_available, build_image (delegated to imagebuild).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RuntimeUnavailable(RuntimeError):
    """Raised when a runtime call is made against an unavailable backend."""


class ContainerRuntime(Protocol):
    def is_available(self) -> bool: ...
    def ensure_running(self, *, name: str, image: str,
                       mounts: list[tuple[str, str, str]],
                       network: str, cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None: ...
    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess: ...
    def status(self, name: str) -> str: ...
    def stop(self, name: str) -> None: ...
    def remove(self, name: str) -> None: ...
    def list_managed(self, prefix: str = "mini_cc-") -> list[str]: ...
    def build_image(self, tag: str, context_dir: Path,
                    dockerfile: Path | None = None) -> None: ...


# ── DockerRuntime ─────────────────────────────────────────────────────────

class DockerRuntime:
    """All docker calls go through subprocess.run. No SDK dep.

    ``prefix`` is prepended to every ``docker`` invocation so Docker
    living inside a WSL2 distro is reachable as ``["wsl", "docker",
    ...]``. Default ``()`` = native docker on PATH. The runtime does
    NOT auto-probe — callers that know the platform (ServerRuntimeContext,
    the CLI sandbox commands) pass the probed prefix in, which keeps
    ``DockerRuntime()`` deterministic for tests.
    """

    def __init__(self, prefix: tuple[str, ...] | list[str] = ()):
        self._prefix = tuple(prefix)

    @property
    def prefix(self) -> tuple[str, ...]:
        return self._prefix

    def _argv(self, *parts: str) -> list[str]:
        from .osdetect import docker_argv
        return docker_argv(self._prefix, *parts)

    def is_available(self) -> bool:
        try:
            cp = subprocess.run(
                self._argv("info", "--format", "{{.ServerVersion}}"),
                capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return cp.returncode == 0

    def status(self, name: str) -> str:
        """One of 'running', 'exited', 'missing'."""
        cp = subprocess.run(
            self._argv("inspect", "--format", "{{.State.Status}}", name),
            capture_output=True, text=True, timeout=10)
        if cp.returncode != 0:
            return "missing"
        return cp.stdout.strip() or "missing"

    def ensure_running(self, *, name: str, image: str,
                       mounts: list[tuple[str, str, str]],
                       network: str, cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None:
        st = self.status(name)
        if st == "running":
            return
        if st == "exited":
            subprocess.run(self._argv("start", name),
                           capture_output=True, text=True, timeout=30)
            return
        argv = self._argv("run", "-d",
                "--name", name,
                "--restart=unless-stopped",
                f"--network={network}")
        for host, container, options in mounts:
            # When docker lives inside WSL, a Windows host path must be
            # its /mnt/<drive>/... view or the bind mount silently fails.
            from .osdetect import to_wsl_path
            host_arg = to_wsl_path(host) if self._prefix else host
            spec = f"{host_arg}:{container}"
            if options:
                spec += f":{options}"
            argv += ["-v", spec]
        if cpu_quota:
            argv += [f"--cpus={cpu_quota}"]
        if memory_limit:
            argv += [f"--memory={memory_limit}"]
        argv.append(image)
        subprocess.run(argv, capture_output=True, text=True,
                       timeout=120, check=True)

    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        argv = self._argv("exec")
        if workdir:
            argv += ["-w", workdir]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [name, "sh", "-c", command]
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def stop(self, name: str) -> None:
        subprocess.run(self._argv("stop", name),
                       capture_output=True, text=True, timeout=30)

    def remove(self, name: str) -> None:
        subprocess.run(self._argv("rm", "-f", name),
                       capture_output=True, text=True, timeout=30)

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        cp = subprocess.run(
            self._argv("ps", "-a", "--format", "{{.Names}}"),
            capture_output=True, text=True, timeout=10)
        if cp.returncode != 0:
            return []
        return [n.strip() for n in cp.stdout.splitlines()
                if n.strip().startswith(prefix)]

    def build_image(self, tag: str, context_dir: Path,
                    dockerfile: Path | None = None) -> None:
        from .imagebuild import build_image
        from .config import ContainerConfig
        cfg = ContainerConfig(
            image_tag=tag,
            dockerfile_path=str(dockerfile) if dockerfile else None)
        build_image(tag, cfg, prefix=self._prefix)


# ── FakeRuntime (test double) ──────────────────────────────────────────────

@dataclass
class FakeRuntime:
    """Records every call. Default available=True, status='missing'."""
    available: bool = True
    initial_status: str = "missing"
    exec_returncode: int = 0
    exec_stdout: str = ""
    exec_stderr: str = ""
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _statuses: dict[str, str] = field(default_factory=dict)

    def is_available(self) -> bool:
        return self.available

    def status(self, name: str) -> str:
        return self._statuses.get(name, self.initial_status)

    def ensure_running(self, *, name, image, mounts, network,
                       cpu_quota=None, memory_limit=None) -> None:
        if not self.available:
            raise RuntimeUnavailable("fake runtime marked unavailable")
        self.calls.append(("ensure_running", dict(
            name=name, image=image, mounts=list(mounts),
            network=network, cpu_quota=cpu_quota, memory_limit=memory_limit)))
        self._statuses[name] = "running"

    def exec(self, *, name, workdir, command, timeout, env):
        if not self.available:
            raise RuntimeUnavailable("fake runtime marked unavailable")
        self.calls.append(("exec", dict(name=name, workdir=workdir,
            command=command, timeout=timeout, env=dict(env))))
        return subprocess.CompletedProcess(
            args=["docker", "exec", name, "sh", "-c", command],
            returncode=self.exec_returncode,
            stdout=self.exec_stdout, stderr=self.exec_stderr)

    def stop(self, name: str) -> None:
        self.calls.append(("stop", dict(name=name)))
        self._statuses[name] = "exited"

    def remove(self, name: str) -> None:
        self.calls.append(("remove", dict(name=name)))
        self._statuses.pop(name, None)

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        return [n for n in self._statuses if n.startswith(prefix)]

    def build_image(self, tag, context_dir, dockerfile=None):
        self.calls.append(("build_image",
            dict(tag=tag, context_dir=context_dir, dockerfile=dockerfile)))
