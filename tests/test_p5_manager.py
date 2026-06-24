"""TenantContainerManager lifecycle. Uses FakeRuntime — no docker daemon."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.sandbox.config import ContainerConfig, Mount
from mini_cc.sandbox.manager import TenantContainerManager, _container_name
from mini_cc.sandbox.runtime import FakeRuntime


def test_container_name_sanitizes_special_chars():
    assert _container_name("tnt_acme.corp") == "mini_cc-tnt_acme.corp"
    assert _container_name("tnt a/b") == "mini_cc-tnt_a_b"
    long = "t" * 80
    name = _container_name(long)
    assert len(name) <= 63
    assert name.startswith("mini_cc-")


def test_container_name_rejects_empty_after_prefix():
    with pytest.raises(ValueError):
        _container_name("")


def test_ensure_running_invokes_runtime_with_expected_args():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1",
        host_projects_dir=Path("/data/tenants/t1/projects"),
        config=ContainerConfig(enabled=True, image_tag="img:v1", network="none"),
        runtime=rt)
    mgr.ensure_running()
    assert len(rt.calls) == 1
    method, kwargs = rt.calls[0]
    assert kwargs["name"] == "mini_cc-t1"
    assert kwargs["image"] == "img:v1"
    assert kwargs["network"] == "none"
    mounts = kwargs["mounts"]
    assert (str(Path("/data/tenants/t1/projects")), "/workspaces", "") in mounts


def test_ensure_running_includes_extra_mounts():
    rt = FakeRuntime()
    cfg = ContainerConfig(enabled=True,
        extra_mounts=[Mount(host="/host/cache", container="/cache", options="ro")])
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"), config=cfg, runtime=rt)
    mgr.ensure_running()
    mounts = rt.calls[0][1]["mounts"]
    assert ("/host/cache", "/cache", "ro") in mounts


def test_ensure_running_passes_resource_limits():
    rt = FakeRuntime()
    cfg = ContainerConfig(enabled=True, cpu_quota="1.5", memory_limit="512m")
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"), config=cfg, runtime=rt)
    mgr.ensure_running()
    kwargs = rt.calls[0][1]
    assert kwargs["cpu_quota"] == "1.5"
    assert kwargs["memory_limit"] == "512m"


def test_ensure_running_idempotent():
    """When status=running, the underlying docker CLI gets no `run` call.
    Verified via DockerRuntime with monkeypatched subprocess — FakeRuntime
    always records ensure_running, so checking its calls list would not
    reflect the docker-level short-circuit."""
    import subprocess
    captured = []
    def fake_run(args, **kw):
        captured.append(args)
        # First (and only) call is `docker inspect` for status; return running.
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="running\n")
    import mini_cc.sandbox.runtime as rt_mod
    orig = rt_mod.subprocess.run
    rt_mod.subprocess.run = fake_run
    try:
        from mini_cc.sandbox.runtime import DockerRuntime
        mgr = TenantContainerManager(
            tid="t1", host_projects_dir=Path("/p"),
            config=ContainerConfig(enabled=True), runtime=DockerRuntime())
        mgr.ensure_running()
        mgr.ensure_running()
    finally:
        rt_mod.subprocess.run = orig
    # Only inspect calls (one per ensure_running). No `docker run`.
    assert all(c[:2] != ["docker", "run"] for c in captured)
    assert all(c[:2] == ["docker", "inspect"] for c in captured)
    assert len(captured) == 2


def test_exec_workdir_uses_container_projects_path():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.exec(project_id="proj_abc", command="ls",
             timeout=30, env={"PATH": "/usr/bin"})
    assert any(c[0] == "exec" for c in rt.calls)
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert exec_kwargs["workdir"] == "/workspaces/proj_abc"
    assert exec_kwargs["command"] == "ls"
    assert exec_kwargs["env"]["PATH"] == "/usr/bin"


def test_exec_does_not_filter_env_caller_decides():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.exec(project_id="p1", command="x", timeout=5, env={"SECRET": "s"})
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert exec_kwargs["env"]["SECRET"] == "s"


def test_stop_delegates_to_runtime():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.ensure_running()
    mgr.stop()
    assert any(c[0] == "stop" for c in rt.calls)


def test_image_tag_passes_through():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True, image_tag="custom:v9"),
        runtime=rt)
    mgr.ensure_running()
    assert rt.calls[0][1]["image"] == "custom:v9"
