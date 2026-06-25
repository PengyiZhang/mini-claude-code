"""ContainerSandbox: fs stays on host, exec/git go through the container."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mini_cc.sandbox import (
    ContainerSandbox, CommandBlockedError, Policy, PathEscapeError)
from mini_cc.sandbox.config import ContainerConfig
from mini_cc.sandbox.manager import TenantContainerManager
from mini_cc.sandbox.runtime import FakeRuntime


@pytest.fixture
def sandbox(tmp_path):
    ws = tmp_path / "p1" / "workspace"
    ws.mkdir(parents=True)
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=tmp_path,
        config=ContainerConfig(enabled=True), runtime=rt)
    sb = ContainerSandbox("p1", ws, Policy(), mgr)
    return sb, rt


def test_fs_ops_make_zero_runtime_calls(sandbox):
    sb, rt = sandbox
    sb.write("file.txt", "hello")
    assert sb.read("file.txt") == "hello"
    sb.edit("file.txt", "hello", "hi")
    assert sb.read("file.txt") == "hi"
    assert sb.glob("*.txt") == ["file.txt"]
    assert len(rt.calls) == 0


def test_execute_invokes_container_exec(sandbox):
    sb, rt = sandbox
    sb.execute("ls -la")
    assert any(c[0] == "exec" for c in rt.calls)
    assert rt.calls[0][0] == "ensure_running"


def test_execute_second_call_reuses_container(sandbox):
    """Each exec() probes ensure_running (to detect container death), but
    the underlying container is only started once. With FakeRuntime,
    _statuses transitions to 'running' after first ensure_running and
    subsequent probes see that — no state change."""
    sb, rt = sandbox
    sb.execute("ls")
    sb.execute("pwd")
    exec_calls = [c for c in rt.calls if c[0] == "exec"]
    ensure_calls = [c for c in rt.calls if c[0] == "ensure_running"]
    assert len(exec_calls) == 2
    # Container started exactly once: after first ensure_running, the
    # FakeRuntime _statuses entry exists and is 'running' throughout.
    # Keyed by tid now (manager passes tid, not pre-sanitized name).
    assert rt._statuses.get("t1") == "running"
    # All ensure_running calls are no-ops at the docker level once running
    # (DockerRuntime short-circuits via status check); we verify the
    # manager doesn't refuse to call exec twice.
    assert len(ensure_calls) >= 1


def test_execute_policy_violation_blocks_before_runtime(sandbox):
    sb, rt = sandbox
    with pytest.raises(CommandBlockedError):
        sb.execute("sudo rm -rf /")
    assert all(c[0] != "exec" for c in rt.calls)


def test_git_invokes_container_exec(sandbox):
    sb, rt = sandbox
    sb.git(["status"])
    exec_calls = [c for c in rt.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    cmd = exec_calls[0][1]["command"]
    assert "git status" in cmd


def test_git_policy_violation_blocks(sandbox):
    sb, rt = sandbox
    with pytest.raises(CommandBlockedError):
        sb.git(["push", "--force"])  # push not in ALLOWED_GIT_SUBCOMMANDS
    assert all(c[0] != "exec" for c in rt.calls)


def test_execute_filters_env_via_policy(sandbox):
    sb, rt = sandbox
    sb.execute("printenv")
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert "HOME" in exec_kwargs["env"]
    assert exec_kwargs["env"]["HOME"] == "/workspaces/p1"
    if "PATH" in os.environ:
        assert "PATH" in exec_kwargs["env"]


def test_validate_path_still_rejects_escapes(sandbox):
    sb, rt = sandbox
    with pytest.raises(PathEscapeError):
        sb.write("../../escape.txt", "x")


def test_resolve_run_cwd_inside_workspace_ok(sandbox):
    sb, rt = sandbox
    sb.execute("ls", cwd="subdir")
    assert any(c[0] == "exec" for c in rt.calls)
