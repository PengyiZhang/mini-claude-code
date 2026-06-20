"""Phase B: storage-layer SessionMeta + index."""
from __future__ import annotations

import json
from pathlib import Path

from mini_cc.storage import FSStorage, SessionMeta


def test_list_sessions_empty_for_new_project(tmp_path):
    st = FSStorage(tmp_path)
    assert st.list_sessions("proj1") == []


def test_save_messages_seeds_session_meta(tmp_path):
    st = FSStorage(tmp_path)
    st.save_messages("proj1", "s1", [{"role": "user", "content": "hi"}])
    metas = st.list_sessions("proj1")
    assert len(metas) == 1
    m = metas[0]
    assert m.session_id == "s1"
    assert m.message_count == 1
    assert m.created_at  # ISO string
    assert m.last_active_at
    assert m.in_memory is False  # default; manager fills this in


def test_save_messages_bumps_existing_meta(tmp_path):
    st = FSStorage(tmp_path)
    st.save_messages("proj1", "s1", [{"role": "user", "content": "a"}])
    first = next(m for m in st.list_sessions("proj1") if m.session_id == "s1")
    st.save_messages("proj1", "s1",
                     [{"role": "user", "content": "a"},
                      {"role": "assistant", "content": "b"},
                      {"role": "user", "content": "c"}])
    metas = {m.session_id: m for m in st.list_sessions("proj1")}
    assert metas["s1"].message_count == 3
    assert metas["s1"].created_at == first.created_at  # stable
    assert metas["s1"].last_active_at >= first.last_active_at


def test_save_and_delete_session_meta_directly(tmp_path):
    st = FSStorage(tmp_path)
    m = SessionMeta(session_id="s2", created_at="2026-01-01T00:00:00Z",
                    last_active_at="2026-01-02T00:00:00Z",
                    message_count=5)
    st.save_session_meta("proj1", m)
    assert any(x.session_id == "s2" for x in st.list_sessions("proj1"))
    st.delete_session_meta("proj1", "s2")
    assert all(x.session_id != "s2" for x in st.list_sessions("proj1"))


def test_delete_session_removes_messages_and_meta(tmp_path):
    st = FSStorage(tmp_path)
    st.save_messages("proj1", "s9", [{"role": "user", "content": "x"}])
    # sanity
    assert any(m.session_id == "s9" for m in st.list_sessions("proj1"))
    st.delete_session("proj1", "s9")
    # gone from index
    assert all(m.session_id != "s9" for m in st.list_sessions("proj1"))
    # message file unlinked too (so back-compat scan wouldn't resurrect)
    assert not (Path(tmp_path) / "proj1" / "messages" / "s9.json").exists()


def test_back_compat_scan_rebuilds_index_from_messages_dir(tmp_path):
    """A project with messages/ but no sessions/index.json (e.g. created
    before Phase B) gets an index on first read."""
    st = FSStorage(tmp_path)
    # Bypass save_messages to simulate a pre-index project.
    proj_dir = Path(tmp_path) / "proj1" / "messages"
    proj_dir.mkdir(parents=True)
    (proj_dir / "old1.json").write_text(
        json.dumps([{"role": "user", "content": "hi"}]), encoding="utf-8")
    (proj_dir / "old2.json").write_text(
        json.dumps([{"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"}]), encoding="utf-8")

    metas = {m.session_id: m for m in st.list_sessions("proj1")}
    assert set(metas) == {"old1", "old2"}
    assert metas["old1"].message_count == 1
    assert metas["old2"].message_count == 2
    # Index file now exists so the next call doesn't rescan.
    assert (Path(tmp_path) / "proj1" / "sessions" / "index.json").exists()


def test_index_isolation_between_projects(tmp_path):
    st = FSStorage(tmp_path)
    st.save_messages("proj1", "s1", [{"role": "user", "content": "x"}])
    st.save_messages("proj2", "s1", [{"role": "user", "content": "y"}])
    p1 = [m.session_id for m in st.list_sessions("proj1")]
    p2 = [m.session_id for m in st.list_sessions("proj2")]
    assert p1 == ["s1"]
    assert p2 == ["s1"]
    # Same session_id, different projects — metas independent.
    m1 = next(m for m in st.list_sessions("proj1") if m.session_id == "s1")
    m2 = next(m for m in st.list_sessions("proj2") if m.session_id == "s1")
    assert m1 is not m2
