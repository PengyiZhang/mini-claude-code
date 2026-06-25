"""sandbox subcommand: build-image / status / stop."""
from __future__ import annotations

import subprocess

import pytest

from mini_cc.sandbox.osdetect import DockerAvailability
from mini_cc.server.cli import (
    cmd_sandbox_build_image, cmd_sandbox_status, cmd_sandbox_stop)


@pytest.fixture(autouse=True)
def _native_probe(monkeypatch):
    """The CLI subcommands call probe_docker() to pick up a WSL argv
    prefix. On a dev machine that actually has WSL2 the probe would
    return ("wsl",) and break these argv-shape assertions. Pin it to a
    native probe so the tests stay platform-independent."""
    monkeypatch.setattr(
        "mini_cc.sandbox.osdetect.probe_docker",
        lambda **kw: DockerAvailability(available=True, argv_prefix=()))


def test_build_image_invokes_docker(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)
    args = type("A", (), {
        "tag": "mini_cc-sandbox:latest",
        "dockerfile": None,
    })()
    rc = cmd_sandbox_build_image(args)
    assert rc == 0
    assert captured[0][:3] == ["docker", "build", "-t"]
    assert captured[0][3] == "mini_cc-sandbox:latest"


def test_status_shows_managed_containers(monkeypatch, capsys):
    def smart_run(args, **kw):
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="mini_cc-t1\nmini_cc-t2\n", stderr="")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="running\n", stderr="")
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="27.5\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", smart_run)
    args = type("A", (), {"tenant_id": None})()
    rc = cmd_sandbox_status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "mini_cc-t1" in out
    assert "mini_cc-t2" in out


def test_status_filters_by_tenant(monkeypatch, capsys):
    def smart_run(args, **kw):
        # Return container list for `docker ps`, status for `docker inspect`,
        # and "ok" for availability probe.
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="mini_cc-t1\nmini_cc-t2\n", stderr="")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="running\n", stderr="")
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args=args, returncode=0,
                stdout="27.5\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", smart_run)
    args = type("A", (), {"tenant_id": "t1"})()
    rc = cmd_sandbox_status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "mini_cc-t1" in out
    assert "mini_cc-t2" not in out


def test_stop_with_tenant_id(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="", stderr=""))
    args = type("A", (), {"tenant_id": "t1"})()
    rc = cmd_sandbox_stop(args)
    assert rc == 0
    assert ["docker", "stop", "mini_cc-t1"] in captured
    assert ["docker", "rm", "-f", "mini_cc-t1"] in captured


def test_status_returns_1_when_docker_unavailable(monkeypatch):
    def boom(a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    args = type("A", (), {"tenant_id": None})()
    rc = cmd_sandbox_status(args)
    assert rc == 1
