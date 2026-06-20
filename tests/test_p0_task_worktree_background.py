"""Task, worktree, and background scheduler tests (no LLM)."""
from __future__ import annotations

import time

import pytest

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext
from mini_cc.tools.background import BackgroundScheduler, is_slow_operation, should_run_background


# ── helpers ───────────────────────────────────────────────────────────────

def _ctx(tmp_path):
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    sandbox.project_root.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_id="proj-a", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[]), sandbox, storage


# ── Task system ───────────────────────────────────────────────────────────

def test_task_create_and_list(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    out = tools["create_task"].handle(ctx, {"subject": "do thing"})
    assert "Created task_" in out
    listed = tools["list_tasks"].handle(ctx, {})
    assert "do thing" in listed


def test_task_list_empty(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    assert tools["list_tasks"].handle(ctx, {}) == "No tasks."


def test_task_claim_blocked_by_uncompleted_dep(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    a_out = tools["create_task"].handle(ctx, {"subject": "A"})
    a_id = a_out.split("Created ")[1].split(":")[0]
    b_out = tools["create_task"].handle(
        ctx, {"subject": "B", "blockedBy": [a_id]})
    b_id = b_out.split("Created ")[1].split(":")[0]
    result = tools["claim_task"].handle(ctx, {"task_id": b_id})
    assert "Cannot start" in result


def test_task_claim_after_dep_completes(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    a_out = tools["create_task"].handle(ctx, {"subject": "A"})
    a_id = a_out.split("Created ")[1].split(":")[0]
    b_out = tools["create_task"].handle(
        ctx, {"subject": "B", "blockedBy": [a_id]})
    b_id = b_out.split("Created ")[1].split(":")[0]
    # claim + complete A first
    assert "Claimed" in tools["claim_task"].handle(ctx, {"task_id": a_id})
    a_complete = tools["complete_task"].handle(ctx, {"task_id": a_id})
    assert "Completed" in a_complete
    assert "Unblocked: B" in a_complete
    # now B can be claimed
    assert "Claimed" in tools["claim_task"].handle(ctx, {"task_id": b_id})


def test_task_claim_missing_dep(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    b_out = tools["create_task"].handle(
        ctx, {"subject": "B", "blockedBy": ["task_missing"]})
    b_id = b_out.split("Created ")[1].split(":")[0]
    result = tools["claim_task"].handle(ctx, {"task_id": b_id})
    assert "missing dep" in result


def test_task_claim_already_owned(tmp_path):
    """Status moves to in_progress after claim; the duplicate-call message
    depends on what comes first. We just need it to reject."""
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    out = tools["create_task"].handle(ctx, {"subject": "A"})
    a_id = out.split("Created ")[1].split(":")[0]
    first = tools["claim_task"].handle(ctx, {"task_id": a_id, "owner": "x"})
    assert "Claimed" in first
    r = tools["claim_task"].handle(ctx, {"task_id": a_id, "owner": "y"})
    assert "cannot claim" in r or "already owned" in r or "in_progress" in r


def test_task_complete_without_claim(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    out = tools["create_task"].handle(ctx, {"subject": "A"})
    a_id = out.split("Created ")[1].split(":")[0]
    r = tools["complete_task"].handle(ctx, {"task_id": a_id})
    assert "cannot complete" in r


def test_task_get_not_found(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    tools = dispatch(builtin_tools())
    r = tools["get_task"].handle(ctx, {"task_id": "task_xxx"})
    assert "not found" in r


# ── Worktrees ─────────────────────────────────────────────────────────────

def _init_git(sandbox):
    sandbox.execute("git init -b main", timeout=30)
    sandbox.execute("git config user.email t@t.com", timeout=10)
    sandbox.execute("git config user.name t", timeout=10)
    sandbox.write("_init.txt", "x")
    sandbox.execute("git add _init.txt", timeout=10)
    sandbox.execute("git commit -m init", timeout=30)


def test_worktree_create_requires_git(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    out = tools["create_worktree"].handle(ctx, {"name": "wt1"})
    assert "created" in out.lower()
    assert (sandbox.project_root / ".worktrees" / "wt1").exists()


def test_worktree_create_invalid_name(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    out = tools["create_worktree"].handle(ctx, {"name": "../escape"})
    assert "Error" in out


def test_worktree_create_duplicate(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    tools["create_worktree"].handle(ctx, {"name": "dup"})
    out = tools["create_worktree"].handle(ctx, {"name": "dup"})
    assert "already exists" in out


def test_worktree_remove_clean(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    tools["create_worktree"].handle(ctx, {"name": "w"})
    out = tools["remove_worktree"].handle(ctx, {"name": "w"})
    assert "removed" in out.lower()


def test_worktree_keep_logs_event(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    tools["create_worktree"].handle(ctx, {"name": "w"})
    out = tools["keep_worktree"].handle(ctx, {"name": "w"})
    assert "kept" in out.lower()
    events = sandbox.project_root / ".worktrees" / "events.jsonl"
    assert events.exists()


def test_worktree_bind_to_task(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    out = tools["create_task"].handle(ctx, {"subject": "T"})
    task_id = out.split("Created ")[1].split(":")[0]
    out = tools["create_worktree"].handle(
        ctx, {"name": "w", "task_id": task_id})
    assert "created" in out.lower()
    task_out = tools["get_task"].handle(ctx, {"task_id": task_id})
    assert "worktree: w" in task_out


def test_worktree_bind_to_missing_task(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    out = tools["create_worktree"].handle(
        ctx, {"name": "w", "task_id": "task_missing"})
    assert "not found" in out


def test_worktree_remove_dirty_rejected(tmp_path):
    ctx, sandbox, _ = _ctx(tmp_path)
    _init_git(sandbox)
    tools = dispatch(builtin_tools())
    tools["create_worktree"].handle(ctx, {"name": "w"})
    wt = sandbox.project_root / ".worktrees" / "w"
    (wt / "new.txt").write_text("x")
    out = tools["remove_worktree"].handle(ctx, {"name": "w"})
    assert "discard_changes=true" in out or "Cannot verify" in out


# ── Background scheduler ──────────────────────────────────────────────────

def test_is_slow_operation_detects_keywords():
    assert is_slow_operation("bash", {"command": "npm install"})
    assert is_slow_operation("bash", {"command": "pytest -x"})
    assert not is_slow_operation("bash", {"command": "echo hi"})
    assert not is_slow_operation("read", {"file_path": "x"})


def test_should_run_background_when_flag_set():
    assert should_run_background(
        "bash", {"command": "echo hi", "run_in_background": True})
    assert not should_run_background(
        "bash", {"command": "echo hi"})


def test_background_start_and_collect():
    bg = BackgroundScheduler()
    sandbox = SubprocessSandbox("p", "/tmp/dummy_does_not_need_to_exist_for_this_test")
    storage = FSStorage("/tmp/dummy_state_path")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                     storage=storage, todos=[])
    handlers = dispatch(builtin_tools())
    bg_id = bg.start(ctx, handlers, "bash", {"command": "echo hello"}, "tu_1")
    assert bg_id.startswith("bg_")
    # Drain notifications (poll briefly)
    deadline = time.time() + 5
    notes = bg.collect_notifications()
    while not notes and time.time() < deadline:
        time.sleep(0.05)
        notes = bg.collect_notifications()
    assert len(notes) == 1
    assert "task_notification" in notes[0]
    assert "completed" in notes[0]
    # Subsequent drain is empty
    assert bg.collect_notifications() == []


def test_background_unknown_tool_handled():
    bg = BackgroundScheduler()
    sandbox = SubprocessSandbox("p", "/tmp/x")
    storage = FSStorage("/tmp/x_state")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                     storage=storage, todos=[])
    bg_id = bg.start(ctx, {}, "nope", {"command": "x"}, "tu")
    deadline = time.time() + 5
    notes = bg.collect_notifications()
    while not notes and time.time() < deadline:
        time.sleep(0.05)
        notes = bg.collect_notifications()
    assert "Unknown tool" in notes[0]
