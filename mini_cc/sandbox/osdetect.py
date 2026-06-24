"""Detect Docker availability and, on Windows, whether WSL2 is the backend.

The probe runs once at process start (cached). Per-call cost is a single
``docker info`` invocation. On Windows, the OperatingSystem field
reveals whether Docker Desktop is using its WSL2 backend (the only
backend that can run Linux containers, which is what the P5 Dockerfile
assumes).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

_CACHE: "DockerAvailability | None" = None


@dataclass(frozen=True)
class DockerAvailability:
    available: bool
    server_version: str = ""
    wsl2: bool = False
    reason: str = ""


def _looks_like_wsl2(os_field: str) -> bool:
    """Heuristic: any of these strings in docker info's OperatingSystem
    field → WSL2 backend. False positives just mean a tenant gets
    ``wsl2=True`` reported, which doesn't drive any code path today."""
    haystack = (os_field or "").lower()
    return any(sig in haystack for sig in (
        "wsl", "docker desktop", "windows subsystem for linux"))


def probe_docker(*, force: bool = False) -> DockerAvailability:
    """Probe ``docker info``. Result is cached for the process lifetime.

    Pass ``force=True`` to bypass the cache (used by tests and the
    ``sandbox status`` CLI command).
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    try:
        cp = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}|{{.OperatingSystem}}"],
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        _CACHE = DockerAvailability(
            available=False, reason="docker binary not found on PATH")
        return _CACHE
    except subprocess.TimeoutExpired:
        _CACHE = DockerAvailability(
            available=False, reason="docker info timed out (>10s)")
        return _CACHE
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout).strip()[:200] or "non-zero exit"
        _CACHE = DockerAvailability(available=False, reason=msg)
        return _CACHE
    parts = cp.stdout.strip().split("|", 1)
    version = parts[0].strip() if parts else ""
    os_field = parts[1].strip() if len(parts) > 1 else ""
    wsl2 = (sys.platform == "win32") and _looks_like_wsl2(os_field)
    _CACHE = DockerAvailability(
        available=True, server_version=version, wsl2=wsl2)
    return _CACHE


def reset_cache() -> None:
    """Test hook — clear the cached probe result."""
    global _CACHE
    _CACHE = None
