"""Detect Docker availability, including the Windows + WSL2 case.

Two ways Docker can be reachable:

- **Native**: the ``docker`` binary is on PATH and talks to a daemon
  directly (Linux, macOS, or Docker Desktop on Windows with its CLI on
  PATH). Invocations are plain ``["docker", ...]``.

- **WSL2 on Windows**: Docker is installed *inside* a WSL2 Linux distro
  rather than as Docker Desktop. There is no ``docker`` on the Windows
  PATH, so we reach it by prefixing every call with ``wsl`` —
  ``["wsl", "docker", ...]`` — which runs in the user's default distro.

Probe sequence on Windows (mirrors how an operator would check by hand):

  1. Confirm the platform is Windows.
  2. ``wsl docker info ...`` — if that succeeds, Docker lives inside the
     default WSL2 distro. (We deliberately do NOT parse ``wsl --list``:
     its output is UTF-16LE on Windows and a pain to decode reliably.
     ``wsl docker info`` is the "merged into one" probe.)
  3. Fall back to native ``docker info`` — catches Docker Desktop, whose
     CLI is on the Windows PATH and whose daemon reports a WSL2 backend.

``DockerAvailability.argv_prefix`` carries the result so the runtime
knows how to build every docker invocation.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

_CACHE: "DockerAvailability | None" = None

# Matches a Windows drive-rooted path:  C:\foo\bar  or  E:/foo/bar
_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


@dataclass(frozen=True)
class DockerAvailability:
    available: bool
    server_version: str = ""
    wsl2: bool = False
    reason: str = ""
    # Argv to prepend to every ``docker ...`` call so it reaches the
    # daemon. Empty tuple = native docker. ("wsl",) = Docker in WSL2.
    argv_prefix: tuple[str, ...] = ()


def _looks_like_wsl2(os_field: str) -> bool:
    """Heuristic: any of these strings in docker info's OperatingSystem
    field → WSL2 backend (Docker Desktop reporting its backend, or a
    distro whose kernel string mentions Microsoft/WSL)."""
    haystack = (os_field or "").lower()
    return any(sig in haystack for sig in (
        "wsl", "docker desktop", "windows subsystem for linux",
        "microsoft"))


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess | None:
    """Run argv, returning None on missing binary or timeout so callers
    can treat "couldn't even invoke it" the same as "not available".

    Output is decoded as UTF-8 with replacement: docker and ``wsl``
    emit UTF-8 (labels, distro names), but on a non-UTF-8 system locale
    (e.g. GBK on Chinese Windows) the default ``text=True`` codec would
    raise UnicodeDecodeError and falsely report "docker unavailable".
    """
    try:
        return subprocess.run(
            argv, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


_INFO_FMT = "{{.ServerVersion}}|{{.OperatingSystem}}"


def _parse_info(stdout: str) -> tuple[str, str]:
    parts = stdout.strip().split("|", 1)
    version = parts[0].strip() if parts else ""
    os_field = parts[1].strip() if len(parts) > 1 else ""
    return version, os_field


def _probe_native() -> DockerAvailability:
    """``docker info`` directly. Returns unavailable if the binary is
    missing or the daemon is down."""
    cp = _run(["docker", "info", "--format", _INFO_FMT], timeout=10)
    if cp is None:
        return DockerAvailability(
            available=False, reason="docker binary not found on PATH")
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout).strip()[:200] or "non-zero exit"
        return DockerAvailability(available=False, reason=msg)
    version, os_field = _parse_info(cp.stdout)
    wsl2 = (sys.platform == "win32") and _looks_like_wsl2(os_field)
    return DockerAvailability(
        available=True, server_version=version, wsl2=wsl2,
        argv_prefix=())


def _probe_windows() -> DockerAvailability:
    """Windows-specific probe. Try Docker-inside-WSL2 first, then native."""
    # Step 2: Docker inside the default WSL2 distro. WSL cold-start can
    # take a while the first time, so allow a generous timeout.
    #
    # NOTE: wsl.exe hands its args to bash for re-parsing, so the format
    # string must NOT contain shell metacharacters — a ``|`` would be
    # read as a pipe (``{{.ServerVersion}}|{{.OperatingSystem}}`` → bash
    # runs ``{{.OperatingSystem}}`` as a command, rc=127). We use a
    # single-field ``docker version`` format which is metachar-free.
    # wsl2 is True by construction here: we only reach docker by
    # tunnelling through WSL, so it's definitionally the WSL2 path.
    cp = _run(["wsl", "docker", "version", "--format", "{{.Server.Version}}"],
              timeout=30)
    if cp is not None and cp.returncode == 0:
        version = cp.stdout.strip()
        return DockerAvailability(
            available=True, server_version=version, wsl2=True,
            argv_prefix=("wsl",),
            reason="docker reached via WSL2 default distro")
    # Step 3: native docker (Docker Desktop CLI on Windows PATH). Goes
    # straight to docker.exe — no bash in between — so the ``|`` in the
    # format string is a literal separator, not a pipe.
    native = _probe_native()
    if native.available:
        return native
    # Neither worked. Prefer the more actionable reason: if ``wsl``
    # itself wasn't found, say so; otherwise surface the native failure.
    wsl_present = _run(["wsl", "--status"], timeout=10)
    if wsl_present is None:
        return DockerAvailability(
            available=False,
            reason="neither `wsl docker` nor native `docker` reachable "
                   "(no `wsl` command and no docker on PATH)")
    return DockerAvailability(
        available=False,
        reason=(f"`wsl` present but `wsl docker version` failed; native "
                f"docker: {native.reason or 'unavailable'}"))


def probe_docker(*, force: bool = False) -> DockerAvailability:
    """Probe Docker once; result cached for the process lifetime.

    On Windows the probe prefers Docker-inside-WSL2 (``wsl docker ...``)
    and falls back to native ``docker``. Elsewhere it probes native
    ``docker`` directly. Pass ``force=True`` to bypass the cache.
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    if sys.platform == "win32":
        _CACHE = _probe_windows()
    else:
        _CACHE = _probe_native()
    return _CACHE


def reset_cache() -> None:
    """Test hook — clear the cached probe result."""
    global _CACHE
    _CACHE = None


def to_wsl_path(path: str) -> str:
    """Translate a Windows drive-rooted path to its WSL2 view.

    ``E:\\foo\\bar`` → ``/mnt/e/foo/bar``. Paths that aren't
    drive-rooted (already-POSIX, UNC, relative) are returned with
    backslashes normalized to forward slashes but otherwise unchanged.
    Used for ``-v`` mount sources and build context when docker runs
    inside WSL — the WSL docker daemon sees Windows drives under
    ``/mnt/<drive>``.
    """
    m = _WIN_DRIVE.match(path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path.replace("\\", "/")


def docker_argv(prefix: tuple[str, ...] | list[str] | None,
                *parts: str) -> list[str]:
    """Build a docker invocation: ``prefix + ["docker", *parts]``."""
    return [*(prefix or ()), "docker", *parts]
