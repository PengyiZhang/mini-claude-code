"""P0 ProjectManager + SessionManager tests (no LLM)."""
from __future__ import annotations

import pytest

from mini_cc.projects import ProjectManager
from mini_cc.session import SessionManager


def test_create_and_get_project(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    p = pm.create(tenant_id="t1", project_id="proj-a", display_name="A")
    assert p.project_id == "proj-a"
    assert p.meta.tenant_id == "t1"
    assert p.workspace.exists()

    p2 = pm.get("proj-a")
    assert p2.project_id == "proj-a"
    assert p2.sandbox.project_root == p.workspace


def test_list_filters_by_tenant(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="a")
    pm.create(tenant_id="t1", project_id="b")
    pm.create(tenant_id="t2", project_id="c")
    assert sorted(p.project_id for p in pm.list("t1")) == ["a", "b"]
    assert [p.project_id for p in pm.list("t2")] == ["c"]


def test_create_duplicate_raises(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="x")
    with pytest.raises(ValueError):
        pm.create(tenant_id="t1", project_id="x")


def test_delete_project(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="x")
    pm.delete("x")
    with pytest.raises(KeyError):
        pm.get("x")


def test_project_workspaces_are_isolated(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pa = pm.create(tenant_id="t1", project_id="a")
    pb = pm.create(tenant_id="t1", project_id="b")
    pa.sandbox.write("file.txt", "from-a")
    pb.sandbox.write("file.txt", "from-b")
    assert pa.sandbox.read("file.txt") == "from-a"
    assert pb.sandbox.read("file.txt") == "from-b"


def test_session_manager_starts_and_lists(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="a")
    sm = SessionManager(pm)
    s = sm.start_session("a", "sess1")
    assert s.session_id == "sess1"
    assert "sess1" in sm.list("a")


def test_session_manager_unknown_project(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    with pytest.raises(KeyError):
        sm.start_session("missing", "sess1")
