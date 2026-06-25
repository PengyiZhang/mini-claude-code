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
    """Windows with Docker Desktop on PATH (no Docker-inside-WSL2):
    the ``wsl docker info`` probe fails, native ``docker info`` succeeds.
    argv_prefix stays () and wsl2 follows the OS field."""
    monkeypatch.setattr(sys, "platform", "win32")

    def smart_run(argv, **kw):
        # The WSL probe runs first and must FAIL so we fall through.
        if argv[:2] == ["wsl", "docker"]:
            return _completed(1, "", "wsl docker not available")
        if argv[:1] == ["wsl"]:
            return _completed(0, "", "")  # wsl --status present
        # Native docker info.
        if argv[:1] == ["docker"]:
            return _completed(0, "27.5|Windows Server 2022")
        return _completed(1, "", "")

    monkeypatch.setattr(subprocess, "run", smart_run)
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is False
    assert avail.argv_prefix == ()
    osdetect.reset_cache()


def test_probe_windows_wsl2_docker_inside_distro(monkeypatch):
    """Docker installed inside the default WSL2 distro (no Docker
    Desktop): ``wsl docker info`` succeeds → argv_prefix=("wsl",)."""
    monkeypatch.setattr(sys, "platform", "win32")

    def smart_run(argv, **kw):
        # Only the WSL path should answer. The probe uses
        # ``wsl docker version --format {{.Server.Version}}`` (single
        # field, no shell metachar — a ``|`` would be read as a pipe by
        # the bash wsl.exe spawns).
        if argv[:3] == ["wsl", "docker", "version"]:
            return _completed(0, "29.1")
        return _completed(1, "", "not found")

    monkeypatch.setattr(subprocess, "run", smart_run)
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is True
    assert avail.argv_prefix == ("wsl",)
    assert avail.server_version == "29.1"
    osdetect.reset_cache()


def test_probe_windows_neither_wsl_nor_docker(monkeypatch):
    """No wsl, no docker on Windows → unavailable with an actionable reason."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(1, "", "nope"))
    from mini_cc.sandbox import osdetect
    osdetect.reset_cache()
    avail = probe_docker()
    assert avail.available is False
    assert avail.argv_prefix == ()
    osdetect.reset_cache()


def test_to_wsl_path_translates_drive_roots():
    from mini_cc.sandbox.osdetect import to_wsl_path
    assert to_wsl_path(r"E:\GitRepos\mini_cc_data") == (
        "/mnt/e/GitRepos/mini_cc_data")
    assert to_wsl_path("C:/foo/bar") == "/mnt/c/foo/bar"
    # Already-POSIX / UNC / relative paths are left alone (slashes normalized).
    assert to_wsl_path("/already/posix") == "/already/posix"
    assert to_wsl_path(r"relative\path") == "relative/path"


def test_docker_argv_prepends_prefix():
    from mini_cc.sandbox.osdetect import docker_argv
    assert docker_argv((), "ps") == ["docker", "ps"]
    assert docker_argv(("wsl",), "ps", "-a") == ["wsl", "docker", "ps", "-a"]
    assert docker_argv(None, "info") == ["docker", "info"]


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
