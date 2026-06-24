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


def test_delete_project_cleans_up_storage_subdir(tmp_path):
    """Regression: delete() used to only rmtree <root>/<pid>/ and left
    <root>/.storage/<pid>/ behind — leaking session messages, todos, cron
    jobs and memory into any future project that reused the same id.

    Tenant-scoped layout: storage now lives at
    <root>/tenants/<tid>/.storage/<pid>/, still per-project isolated."""
    pm = ProjectManager(tmp_path / "projects")
    p = pm.create(tenant_id="t1", project_id="x")
    p.storage.save_messages("x", "s1", [{"role": "user", "content": "hi"}])
    storage_dir = (
        tmp_path / "projects" / "tenants" / "t1" / ".storage" / "x")
    assert storage_dir.exists(), "sanity: storage subdir should exist"

    pm.delete("x")

    assert not storage_dir.exists(), (
        "storage subdir must be cleaned up on delete to avoid cross-tenant "
        "data leak when the project_id is reused")


def test_delete_project_then_recreate_starts_clean(tmp_path):
    """A fresh project with the same id must not inherit the deleted
    project's sessions, messages, or memory."""
    pm = ProjectManager(tmp_path / "projects")
    p1 = pm.create(tenant_id="t1", project_id="x")
    p1.storage.save_messages("x", "s1", [{"role": "user", "content": "secret"}])
    p1.storage.append_memory("x", "leaked memory line")
    pm.delete("x")

    p2 = pm.create(tenant_id="t2", project_id="x")  # different tenant
    assert p2.storage.load_messages("x", "s1") == [], (
        "messages from the previous tenant must not leak into a re-created project")
    assert p2.storage.load_memory("x").strip() == "", (
        "memory from the previous tenant must not leak into a re-created project")
    assert p2.storage.list_sessions("x") == [], (
        "session index from the previous tenant must not leak")


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

