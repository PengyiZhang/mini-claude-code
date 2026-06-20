"""Phase B: SessionManager resume paths + repair_dangling_tool_uses."""
from __future__ import annotations

import pytest

from mini_cc.core.loop import (INTERRUPTED_TOOL_RESULT, AgentLoop,
                               repair_dangling_tool_uses)
from mini_cc.projects import ProjectManager
from mini_cc.session import SessionManager


# ── repair_dangling_tool_uses ───────────────────────────────────────────────

def test_repair_clean_transcript_noop():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert repair_dangling_tool_uses(msgs) is False
    assert len(msgs) == 2


def test_repair_appends_synthetic_tool_result_for_dangling_tool_use():
    msgs = [
        {"role": "user", "content": "do thing"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "tu_1", "name": "bash",
             "input": {"command": "ls"}},
        ]},
    ]
    assert repair_dangling_tool_uses(msgs) is True
    assert len(msgs) == 3
    tail = msgs[-1]
    assert tail["role"] == "user"
    assert isinstance(tail["content"], list)
    assert tail["content"][0]["tool_use_id"] == "tu_1"
    assert tail["content"][0]["content"] == INTERRUPTED_TOOL_RESULT
    assert tail["content"][0]["is_error"] is True


def test_repair_skips_when_tool_result_already_present():
    """Mid-transcript tool_use with a matching tool_result is left alone
    even if it's the most recent assistant turn's neighbor."""
    msgs = [
        {"role": "user", "content": "do thing"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "bash",
             "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": "file1\nfile2"},
        ]},
    ]
    assert repair_dangling_tool_uses(msgs) is False
    assert len(msgs) == 3  # unchanged


def test_repair_noop_on_trailing_user_message():
    msgs = [
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "content": "again"},
    ]
    assert repair_dangling_tool_uses(msgs) is False


def test_repair_handles_multiple_dangling_tool_uses():
    msgs = [
        {"role": "user", "content": "do two things"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_a", "name": "bash",
             "input": {"command": "ls"}},
            {"type": "tool_use", "id": "tu_b", "name": "fs_read",
             "input": {"path": "f"}},
        ]},
    ]
    assert repair_dangling_tool_uses(msgs) is True
    assert len(msgs[-1]["content"]) == 2
    ids = {b["tool_use_id"] for b in msgs[-1]["content"]}
    assert ids == {"tu_a", "tu_b"}


# ── SessionManager resume ────────────────────────────────────────────────────

def _make(tmp_path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="proj")
    sm = SessionManager(pm)
    return pm, sm


def test_list_includes_in_memory_flag(tmp_path):
    pm, sm = _make(tmp_path)
    sm.start_session("proj", "warm1")
    metas = {m.session_id: m for m in sm.list("proj")}
    assert "warm1" in metas
    assert metas["warm1"].in_memory is True


def test_list_after_disk_only_shows_in_memory_false(tmp_path):
    pm, sm = _make(tmp_path)
    # Seed a session on disk via storage directly (simulates a session
    # that was active before a restart).
    project = pm.get("proj")
    project.storage.save_messages("proj", "cold1",
                                  [{"role": "user", "content": "hi"}])
    metas = {m.session_id: m for m in sm.list("proj")}
    assert "cold1" in metas
    assert metas["cold1"].in_memory is False


def test_ensure_warm_cold_loads_then_returns_same_instance(tmp_path):
    pm, sm = _make(tmp_path)
    project = pm.get("proj")
    project.storage.save_messages("proj", "cold2",
                                  [{"role": "user", "content": "hi"}])
    first = sm._ensure_warm("proj", "cold2")
    second = sm._ensure_warm("proj", "cold2")
    assert first is second  # same Session object
    # list now reflects warm state
    metas = {m.session_id: m for m in sm.list("proj")}
    assert metas["cold2"].in_memory is True


def test_ensure_warm_unknown_raises_keyerror(tmp_path):
    pm, sm = _make(tmp_path)
    with pytest.raises(KeyError):
        sm._ensure_warm("proj", "nope")


def test_start_session_with_existing_id_is_idempotent_resume(tmp_path):
    pm, sm = _make(tmp_path)
    s_first = sm.start_session("proj", "s_dup")
    project = pm.get("proj")
    # Persist some messages so there's a transcript to resume.
    project.storage.save_messages("proj", "s_dup",
                                  [{"role": "user", "content": "hi"},
                                   {"role": "assistant",
                                    "content": [{"type": "text", "text": "yo"}]}])
    # Drop from memory to simulate restart.
    sm._sessions.pop(("proj", "s_dup"), None)
    s_resumed = sm.start_session("proj", "s_dup")
    # Same session_id, fresh AgentLoop, loaded transcript from disk.
    assert isinstance(s_resumed.session_id, str)
    assert s_resumed.session_id == "s_dup"
    assert len(s_resumed.loop.messages) == 2


def test_warm_repairs_dangling_tool_use(tmp_path):
    pm, sm = _make(tmp_path)
    project = pm.get("proj")
    project.storage.save_messages("proj", "crashed", [
        {"role": "user", "content": "do"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_x", "name": "bash",
             "input": {"command": "ls"}},
        ]},
    ])
    # Cold resume should trigger repair.
    sess = sm._ensure_warm("proj", "crashed")
    assert len(sess.loop.messages) == 3
    tail = sess.loop.messages[-1]
    assert tail["role"] == "user"
    assert tail["content"][0]["content"] == INTERRUPTED_TOOL_RESULT
    # Repair persisted to disk.
    reloaded = project.storage.load_messages("proj", "crashed")
    assert len(reloaded) == 3
    assert reloaded[-1]["content"][0]["content"] == INTERRUPTED_TOOL_RESULT


def test_remove_clears_disk_and_memory(tmp_path):
    pm, sm = _make(tmp_path)
    sm.start_session("proj", "bye")
    assert sm.remove("proj", "bye") is True
    # Both memory and disk are clean
    assert ("proj", "bye") not in sm._sessions
    assert all(m.session_id != "bye" for m in sm.list("proj"))
    # _ensure_warm no longer finds it
    with pytest.raises(KeyError):
        sm._ensure_warm("proj", "bye")


def test_simulated_restart_resumes_session(tmp_path):
    """Two SessionManagers against the same data dir: cold start with
    session created in the first, then a 'restart' — second manager
    sees the session on disk and can resume."""
    pm1 = ProjectManager(tmp_path / "projects")
    pm1.create(tenant_id="t1", project_id="proj")
    sm1 = SessionManager(pm1)
    sm1.start_session("proj", "persist_me")
    project1 = pm1.get("proj")
    project1.storage.save_messages("proj", "persist_me", [
        {"role": "user", "content": "hi"},
        {"role": "assistant",
         "content": [{"type": "text", "text": "hello"}]},
    ])

    # "Restart": fresh managers, same data dir.
    pm2 = ProjectManager(tmp_path / "projects")
    sm2 = SessionManager(pm2)
    metas = {m.session_id: m for m in sm2.list("proj")}
    assert "persist_me" in metas
    assert metas["persist_me"].in_memory is False
    sess = sm2._ensure_warm("proj", "persist_me")
    assert len(sess.loop.messages) == 2
    assert sess.loop.messages[0]["content"] == "hi"
