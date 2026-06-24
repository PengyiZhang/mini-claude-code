"""End-to-end: when container enabled but docker unavailable, the
project auto-degrades to SubprocessSandbox (no 503)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.auth import TenantKeyRegistry
from mini_cc.sandbox import SubprocessSandbox, ContainerSandbox
from mini_cc.server.runtime_context import ServerRuntimeContext


def _build(tmp_path, monkeypatch, *, docker_available: bool, runtime=None):
    monkeypatch.setenv("MINI_CC_DATA_DIR", str(tmp_path))
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text("enabled = true\n")

    ctx = ServerRuntimeContext(
        data_dir=tmp_path,
        key_registry=TenantKeyRegistry(tmp_path / "keys.json"),
        docker_available=docker_available,
        _runtime=runtime,
    )
    pm = ctx.build_project_manager()
    return ctx, pm


def test_tenant_with_container_enabled_and_docker_available(tmp_path, monkeypatch):
    from mini_cc.sandbox.runtime import FakeRuntime
    ctx, pm = _build(tmp_path, monkeypatch, docker_available=True, runtime=FakeRuntime())
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, ContainerSandbox)


def test_tenant_with_container_enabled_but_docker_missing_degrades(tmp_path, monkeypatch):
    """User requirement #2: container env unsupported → degrade to current."""
    ctx, pm = _build(tmp_path, monkeypatch, docker_available=False)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)
    # The degrade reason is recorded on the runtime context for /metrics.
    assert any(d.tenant_id == "t1" for d in ctx.degrades)


def test_tenant_without_config_always_subprocess(tmp_path, monkeypatch):
    """No sandbox.toml, no MINI_CC_SANDBOX_DEFAULT → subprocess always."""
    monkeypatch.delenv("MINI_CC_SANDBOX_DEFAULT", raising=False)
    monkeypatch.setenv("MINI_CC_DATA_DIR", str(tmp_path))
    ctx = ServerRuntimeContext(
        data_dir=tmp_path,
        key_registry=TenantKeyRegistry(tmp_path / "keys.json"),
        docker_available=True,
    )
    pm = ctx.build_project_manager()
    p = pm.create(tenant_id="t_no_sb", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)


def test_shutdown_stops_managed_containers(tmp_path, monkeypatch):
    """On shutdown, every container manager gets stop() called."""
    from mini_cc.sandbox.runtime import FakeRuntime
    rt = FakeRuntime()
    ctx, pm = _build(tmp_path, monkeypatch, docker_available=True, runtime=rt)
    p = pm.create(tenant_id="t1", project_id="p1")
    # Trigger an exec so the container manager is wired into ctx.
    p.sandbox.execute("ls")
    ctx.shutdown()
    stop_calls = [c for c in rt.calls if c[0] == "stop"]
    assert len(stop_calls) >= 1


def test_second_project_same_tenant_reuses_container_manager(tmp_path, monkeypatch):
    """Per-tenant container: two projects under one tenant share one mgr."""
    from mini_cc.sandbox.runtime import FakeRuntime
    rt = FakeRuntime()
    ctx, pm = _build(tmp_path, monkeypatch, docker_available=True, runtime=rt)
    p1 = pm.create(tenant_id="t1", project_id="p1")
    p2 = pm.create(tenant_id="t1", project_id="p2")
    assert isinstance(p1.sandbox, ContainerSandbox)
    assert isinstance(p2.sandbox, ContainerSandbox)
    # Same manager instance for both projects.
    assert p1.sandbox._mgr is p2.sandbox._mgr
