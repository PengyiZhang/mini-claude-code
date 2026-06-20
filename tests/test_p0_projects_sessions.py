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
    ids = [m.session_id for m in sm.list("a")]
    assert "sess1" in ids


def test_session_manager_unknown_project(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    with pytest.raises(KeyError):
        sm.start_session("missing", "sess1")


# ── P3: HTTP-driven helpers ─────────────────────────────────────────

def test_session_remove_unregisters_and_stops(tmp_path):
    """SessionManager.remove stops the loop AND drops the registration."""
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="a")
    sm = SessionManager(pm)
    sm.start_session("a", "sess1")
    ids = [m.session_id for m in sm.list("a")]
    assert "sess1" in ids
    assert sm.remove("a", "sess1") is True
    ids_after = [m.session_id for m in sm.list("a")]
    assert "sess1" not in ids_after


def test_session_remove_unknown_returns_false(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="a")
    sm = SessionManager(pm)
    assert sm.remove("a", "nope") is False


def test_session_try_lock_reports_busy(tmp_path):
    """When another caller holds the project lock, try_lock returns False."""
    import threading
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="a")
    sm = SessionManager(pm)
    held = threading.Event()
    release = threading.Event()

    def hold():
        lock = sm._lock_for("a")
        with lock:
            held.set()
            release.wait(timeout=2)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(timeout=2)
    try:
        # Project is busy → try_lock returns False
        assert sm.try_lock("a") is False
    finally:
        release.set()
        t.join()
    # Now free again
    assert sm.try_lock("a") is True


def test_project_id_rejects_traversal(tmp_path):
    """Invalid project_id characters (path separators, dots) are rejected."""
    pm = ProjectManager(tmp_path / "projects")
    for bad in ["../etc", "a/b", "a b", "a:b", "a\nb"]:
        with pytest.raises(ValueError):
            pm.create(tenant_id="t1", project_id=bad)


def test_project_id_accepts_safe_chars(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    for ok in ["a", "proj_a-b", "proj123", "A_B-C"]:
        pm.create(tenant_id="t1", project_id=ok)
        assert pm.get(ok).project_id == ok

