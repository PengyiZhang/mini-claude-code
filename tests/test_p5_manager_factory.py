"""ProjectManager accepts a sandbox_factory closure. Default preserves
today's behaviour (SubprocessSandbox)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.projects import ProjectManager
from mini_cc.sandbox import ContainerSandbox, SubprocessSandbox, Policy
from mini_cc.sandbox.config import ContainerConfig
from mini_cc.sandbox.manager import TenantContainerManager
from mini_cc.sandbox.runtime import FakeRuntime


def test_default_factory_returns_subprocess(tmp_path):
    pm = ProjectManager(tmp_path)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)
    assert not isinstance(p.sandbox, ContainerSandbox)


def test_custom_factory_used_when_supplied(tmp_path):
    rt = FakeRuntime()
    captured = []

    def factory(tid, pid, ws, policy):
        captured.append((tid, pid, ws))
        mgr = TenantContainerManager(
            tid=tid, host_projects_dir=ws.parent,
            config=ContainerConfig(enabled=True), runtime=rt)
        return ContainerSandbox(pid, ws, policy, mgr)

    pm = ProjectManager(tmp_path, sandbox_factory=factory)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, ContainerSandbox)
    assert captured[0][0] == "t1"
    assert captured[0][1] == "p1"


def test_factory_receives_tenant_id_and_workspace(tmp_path):
    """Factory gets (tenant_id, project_id, workspace, policy) so it can
    build tenant-scoped container managers."""
    rt = FakeRuntime()
    seen_args = []

    def factory(tid, pid, ws, policy):
        seen_args.append((tid, pid, ws, policy))
        mgr = TenantContainerManager(
            tid=tid, host_projects_dir=ws.parent,
            config=ContainerConfig(enabled=True), runtime=rt)
        return ContainerSandbox(pid, ws, policy, mgr)

    pm = ProjectManager(tmp_path, sandbox_factory=factory)
    pm.create(tenant_id="tenant_x", project_id="proj_y")
    assert seen_args[0][0] == "tenant_x"
    assert seen_args[0][1] == "proj_y"
    assert isinstance(seen_args[0][2], Path)
    assert isinstance(seen_args[0][3], Policy)
