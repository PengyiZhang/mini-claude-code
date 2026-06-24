"""FakeRuntime records calls so tests can verify docker interactions
without a real daemon. Also tests DockerRuntime's command construction
(subprocess.run is patched — we only check the argv shape)."""
from __future__ import annotations

import subprocess

import pytest

from mini_cc.sandbox.runtime import (
    ContainerRuntime, DockerRuntime, FakeRuntime, RuntimeUnavailable)


# ── FakeRuntime ────────────────────────────────────────────────────────────

def test_fake_runtime_default_unavailable():
    rt = FakeRuntime(available=False)
    with pytest.raises(RuntimeUnavailable):
        rt.ensure_running(name="x", image="img", mounts=[], network="none")


def test_fake_runtime_records_run_call():
    rt = FakeRuntime(available=True)
    rt.ensure_running(name="mini_cc-t1", image="img", mounts=[], network="none")
    assert len(rt.calls) == 1
    method, kwargs = rt.calls[0]
    assert method == "ensure_running"
    assert kwargs["name"] == "mini_cc-t1"


def test_fake_runtime_status_running_skips_ensure():
    rt = FakeRuntime(available=True, initial_status="running")
    rt.ensure_running(name="x", image="img", mounts=[], network="none")
    assert all(c[0] != "run" for c in rt.calls)


def test_fake_runtime_exec_returns_completed():
    rt = FakeRuntime(available=True)
    cp = rt.exec(name="x", workdir="/w", command="ls", timeout=10, env={})
    assert cp.returncode == 0
    assert isinstance(cp, subprocess.CompletedProcess)


def test_fake_runtime_exec_timeout_returns_124():
    rt = FakeRuntime(available=True, exec_returncode=124)
    cp = rt.exec(name="x", workdir="/w", command="sleep 5", timeout=1, env={})
    assert cp.returncode == 124


# ── DockerRuntime command construction ─────────────────────────────────────

def test_docker_runtime_is_available_true(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="27.5"))
    assert DockerRuntime().is_available() is True


def test_docker_runtime_is_available_false_when_docker_missing(monkeypatch):
    def boom(a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    assert DockerRuntime().is_available() is False


def test_docker_runtime_run_command_shape(monkeypatch):
    captured = []
    def fake(args, **kw):
        captured.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="")
    monkeypatch.setattr(subprocess, "run", fake)
    rt = DockerRuntime()
    rt.ensure_running(name="mini_cc-t1", image="img",
                      mounts=[("/host/w", "/workspaces", "")],
                      network="none")
    run_calls = [c for c in captured if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    argv = run_calls[0]
    assert "--network=none" in argv
    assert "-v" in argv
    v_idx = argv.index("-v")
    assert argv[v_idx + 1] == "/host/w:/workspaces"
    assert "img" in argv
    assert "--name" in argv
    n_idx = argv.index("--name")
    assert argv[n_idx + 1] == "mini_cc-t1"


def test_docker_runtime_exec_command_shape(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="ok"))
    rt = DockerRuntime()
    rt.exec(name="mini_cc-t1", workdir="/workspaces/p1",
            command="ls -la", timeout=30, env={"FOO": "bar"})
    exec_calls = [c for c in captured if c[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 1
    argv = exec_calls[0]
    assert "-w" in argv
    w_idx = argv.index("-w")
    assert argv[w_idx + 1] == "/workspaces/p1"
    assert "-e" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "FOO=bar"
    assert "mini_cc-t1" in argv
    # Last 3 are `sh -c "ls -la"`
    assert argv[-3:] == ["sh", "-c", "ls -la"]


def test_docker_runtime_status_returns_string(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="running\n"))
    assert DockerRuntime().status("mini_cc-t1") == "running"


def test_docker_runtime_status_missing(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=1, stderr="No such object"))
    assert DockerRuntime().status("mini_cc-t1") == "missing"


def test_docker_runtime_stop_and_remove(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(args=a, returncode=0))
    rt = DockerRuntime()
    rt.stop("mini_cc-t1")
    rt.remove("mini_cc-t1")
    assert ["docker", "stop", "mini_cc-t1"] in captured
    assert ["docker", "rm", "-f", "mini_cc-t1"] in captured


def test_docker_runtime_list_managed(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0,
            stdout="mini_cc-t1\nmini_cc-t2\nother-container\n"))
    rt = DockerRuntime()
    names = rt.list_managed()
    assert names == ["mini_cc-t1", "mini_cc-t2"]
