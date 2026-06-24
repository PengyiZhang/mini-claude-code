"""OS / Docker availability probe. No real docker daemon needed — the
probe is patched with monkeypatch in every test."""
from __future__ import annotations

import subprocess
import sys

from mini_cc.sandbox.osdetect import DockerAvailability, probe_docker


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker", "info"], returncode=returncode,
        stdout=stdout, stderr=stderr)


def test_probe_success_linux(monkeypatch):
    """docker info returns 0 → available, wsl2=False on non-Windows."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, "27.5|Docker Engine"))
    monkeypatch.setattr(sys, "platform", "linux")
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is True
    assert avail.server_version == "27.5"
    assert avail.wsl2 is False
    assert avail.reason == ""
    osdetect.reset_cache()


def test_probe_docker_missing(monkeypatch):
    """docker binary not on PATH → FileNotFoundError → not available."""
    def boom(*a, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", boom)
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is False
    assert "docker" in avail.reason.lower() or "not found" in avail.reason.lower()
    osdetect.reset_cache()


def test_probe_daemon_dead(monkeypatch):
    """docker exists but daemon not running → returncode != 0 → unavailable."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(1, "", "Cannot connect to the Docker daemon"))
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is False
    assert "daemon" in avail.reason.lower() or "cannot connect" in avail.reason.lower()
    osdetect.reset_cache()


def test_probe_wsl2_backend(monkeypatch):
    """On Windows, docker info output reveals WSL2 backend."""
    monkeypatch.setattr(sys, "platform", "win32")
    info = "27.5|Docker Desktop"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, info))
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is True
    osdetect.reset_cache()


def test_probe_windows_native(monkeypatch):
    """Windows native containers (no WSL2) → wsl2=False, available still True."""
    monkeypatch.setattr(sys, "platform", "win32")
    info = "27.5|Windows Server 2022"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, info))
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is False
    osdetect.reset_cache()


def test_probe_caches_no_repeated_calls(monkeypatch):
    """Second call returns cached result without invoking subprocess again."""
    calls = []
    def counting(*a, **kw):
        calls.append(1)
        return _completed(0, "27.5|Docker Engine")
    monkeypatch.setattr(subprocess, "run", counting)
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    probe_docker()
    probe_docker()
    assert len(calls) == 1
    osdetect.reset_cache()
