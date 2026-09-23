"""Tenant-scoped project layout: paths must include tenant_id so two tenants
using the same project_id never collide on disk."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.projects import ProjectManager
from mini_cc.projects.layout import (
    find_meta, list_project_ids, meta_path, project_dir, read_meta,
    state_path, tenant_projects_dir, tenant_storage_dir, workspace_path,
    write_meta, ProjectMeta)


# ── Pure layout functions ─────────────────────────────────────────────

def test_project_dir_includes_tenant_layer(tmp_path):
    p = project_dir(tmp_path, "t1", "p1")
    assert p == tmp_path / "tenants" / "t1" / "projects" / "p1"


def test_workspace_state_meta_all_under_tenant_scope(tmp_path):
    tid, pid = "t1", "p1"
    assert workspace_path(tmp_path, tid, pid) == (
        tmp_path / "tenants" / tid / "projects" / pid / "workspace")
    assert state_path(tmp_path, tid, pid) == (
        tmp_path / "tenants" / tid / "projects" / pid / ".state")
    assert meta_path(tmp_path, tid, pid) == (
        tmp_path / "tenants" / tid / "projects" / pid / "meta.json")


def test_tenant_storage_dir_is_per_tenant(tmp_path):
    assert tenant_storage_dir(tmp_path, "t1") == (
        tmp_path / "tenants" / "t1" / ".storage")
    assert tenant_storage_dir(tmp_path, "t2") == (
        tmp_path / "tenants" / "t2" / ".storage")


def test_tenant_projects_dir(tmp_path):
    assert tenant_projects_dir(tmp_path, "t1") == (
        tmp_path / "tenants" / "t1" / "projects")


def test_write_and_read_meta_round_trip(tmp_path):
    meta = ProjectMeta(
        project_id="p1", tenant_id="t1",
        created_at="2026-06-24T12:00:00", display_name="P1")
    write_meta(tmp_path, meta)
    assert meta_path(tmp_path, "t1", "p1").exists()

    got = read_meta(tmp_path, "t1", "p1")
    assert got is not None
    assert got.project_id == "p1"
    assert got.tenant_id == "t1"


def test_read_meta_missing_returns_none(tmp_path):
    assert read_meta(tmp_path, "t1", "missing") is None


def test_find_meta_scans_tenants(tmp_path):
    """Given only project_id, find_meta locates the owning tenant."""
    write_meta(tmp_path, ProjectMeta(
        project_id="p1", tenant_id="t_x",
        created_at="2026-06-24T12:00:00"))
    found = find_meta(tmp_path, "p1")
    assert found is not None
    assert found.tenant_id == "t_x"
    assert found.project_id == "p1"


def test_find_meta_unknown_returns_none(tmp_path):
    assert find_meta(tmp_path, "nope") is None


def test_list_project_ids_scoped_to_one_tenant(tmp_path):
    write_meta(tmp_path, ProjectMeta(
        project_id="a", tenant_id="t1", created_at="x"))
    write_meta(tmp_path, ProjectMeta(
        project_id="b", tenant_id="t1", created_at="x"))
    write_meta(tmp_path, ProjectMeta(
        project_id="c", tenant_id="t2", created_at="x"))
    assert sorted(list_project_ids(tmp_path, "t1")) == ["a", "b"]
    assert list_project_ids(tmp_path, "t2") == ["c"]


def test_list_project_ids_unscoped_walks_all_tenants(tmp_path):
    write_meta(tmp_path, ProjectMeta(
        project_id="a", tenant_id="t1", created_at="x"))
    write_meta(tmp_path, ProjectMeta(
        project_id="b", tenant_id="t2", created_at="x"))
    assert sorted(list_project_ids(tmp_path)) == ["a", "b"]


# ── ProjectManager integration ────────────────────────────────────────

def test_pm_creates_tenant_scoped_paths(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="p1")
    assert (tmp_path / "tenants" / "t1" / "projects" / "p1" / "workspace").exists()
    assert (tmp_path / "tenants" / "t1" / "projects" / "p1" / "meta.json").exists()


def test_pm_two_tenants_same_pid_no_collision(tmp_path):
    """Two tenants using project_id 'x' must get separate on-disk dirs
    AND separate storage roots."""
    pm = ProjectManager(tmp_path)
    pa = pm.create(tenant_id="t1", project_id="x")
    pb = pm.create(tenant_id="t2", project_id="x")
    pa.sandbox.write("file.txt", "from-t1")
    pb.sandbox.write("file.txt", "from-t2")
    assert pa.sandbox.read("file.txt") == "from-t1"
    assert pb.sandbox.read("file.txt") == "from-t2"

    pa.storage.save_messages("x", "s1", [{"role": "user", "content": "t1-msg"}])
    pb.storage.save_messages("x", "s1", [{"role": "user", "content": "t2-msg"}])
    assert pa.storage.load_messages("x", "s1")[0]["content"] == "t1-msg"
    assert pb.storage.load_messages("x", "s1")[0]["content"] == "t2-msg"


def test_pm_get_requires_tenant_id_with_get_any_escape(tmp_path):
    """S3 guard: bare get() must not scan tenants. get_any() is the
    explicit cross-tenant lookup for internal tooling."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="p1")
    with pytest.raises(ValueError, match="tenant_id"):
        pm.get("p1")
    assert pm.get_any("p1").meta.tenant_id == "t1"


def test_pm_get_with_tenant_id_is_scoped(tmp_path):
    """When caller supplies tenant_id, get() only looks in that tenant."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="x")
    pm.create(tenant_id="t2", project_id="x")
    p = pm.get("x", tenant_id="t2")
    assert p.meta.tenant_id == "t2"


def test_pm_delete_with_tenant_id_targets_only_that_tenant(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="x")
    pm.create(tenant_id="t2", project_id="x")
    pm.delete("x", tenant_id="t1")

    assert not (tmp_path / "tenants" / "t1" / "projects" / "x").exists()
    assert (tmp_path / "tenants" / "t2" / "projects" / "x").exists()

    # t2's project must still be retrievable.
    p = pm.get("x", tenant_id="t2")
    assert p.meta.tenant_id == "t2"


def test_pm_delete_unspecified_tid_raises_regardless_of_ambiguity(tmp_path):
    """S3 guard: delete(pid) without tid always raises — a destructive
    call must never scan tenants, ambiguous or not."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="x")
    pm.create(tenant_id="t2", project_id="x")
    with pytest.raises(ValueError, match="tenant_id"):
        pm.delete("x")
    # Both tenants' data survive
    assert pm.get("x", tenant_id="t1") is not None
    assert pm.get("x", tenant_id="t2") is not None


# ── ServerRuntimeContext mount path ───────────────────────────────────

def test_runtime_context_mount_path_is_tenant_scoped(tmp_path):
    """ServerRuntimeContext must mount <data>/tenants/<tid>/projects into
    the container — NOT <data>/projects (the bug we're fixing)."""
    from mini_cc.auth import TenantKeyRegistry
    from mini_cc.sandbox.runtime import FakeRuntime
    from mini_cc.server.runtime_context import ServerRuntimeContext

    tenants_dir = tmp_path / "tenants"
    t1_dir = tenants_dir / "t1"
    t1_dir.mkdir(parents=True)
    (t1_dir / "sandbox.toml").write_text('enabled = true\n')

    ctx = ServerRuntimeContext(
        data_dir=tmp_path,
        key_registry=TenantKeyRegistry(tmp_path / "keys.json"),
        docker_available=True,
        _runtime=FakeRuntime(),
    )

    ws = tmp_path / "tenants" / "t1" / "projects" / "p1" / "workspace"
    ws.mkdir(parents=True)
    from mini_cc.sandbox import Policy
    ctx._sandbox_factory("t1", "p1", ws, Policy())

    mgr = ctx._container_mgrs["t1"]
    assert mgr.host_projects_dir == tmp_path / "tenants" / "t1" / "projects", (
        f"expected tenant-scoped mount, got {mgr.host_projects_dir}")
