"""Round 2 audit Batch 3 tests — atomicity, corruption detection,
MCP close, and background subprocess kill (A1, A2, A3, A4)."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from mini_cc.storage import FSStorage, StorageCorruptionError, Task
from mini_cc.mcp import MCPPool
from mini_cc.mcp.client import MCPClient
from mini_cc.tools.background import BackgroundScheduler
from mini_cc.tools.base import ToolContext
from mini_cc.tools.bash import BASH_TOOL
from mini_cc.sandbox.subprocess_sandbox import SubprocessSandbox
from mini_cc.sandbox.policy import Policy


# ── A1: atomic writes ──────────────────────────────────────────────────────

def test_save_messages_is_atomic(tmp_path, monkeypatch):
    """If _atomic_write_json fails mid-replace, the prior file must be
    intact — not truncated."""
    s = FSStorage(tmp_path / "state")
    msgs = [{"role": "user", "content": "first"}]
    s.save_messages("p", "s", msgs)
    fp = tmp_path / "state" / "p" / "messages" / "s.json"
    original = fp.read_text(encoding="utf-8")

    # Sabotage os.replace so the second save blows up after the tmp
    # file is written but before the atomic swap.
    real_replace = os.replace

    def boom(*args, **kwargs):
        raise OSError("simulated mid-write crash")

    monkeypatch.setattr("mini_cc.storage.fs.os.replace", boom)
    with pytest.raises(OSError):
        s.save_messages("p", "s", [{"role": "user", "content": "second"}])
    monkeypatch.undo()

    # The original file must still be readable and unchanged.
    assert fp.read_text(encoding="utf-8") == original
    assert s.load_messages("p", "s") == msgs


def test_save_todos_atomic(tmp_path, monkeypatch):
    s = FSStorage(tmp_path / "state")
    s.save_todos("p", "s", [{"content": "x", "status": "pending"}])
    fp = tmp_path / "state" / "p" / "todos" / "s.json"
    original = fp.read_text(encoding="utf-8")

    monkeypatch.setattr("mini_cc.storage.fs.os.replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        s.save_todos("p", "s", [{"content": "y", "status": "done"}])
    assert fp.read_text(encoding="utf-8") == original


def test_save_task_atomic(tmp_path, monkeypatch):
    s = FSStorage(tmp_path / "state")
    t = Task(id="t1", subject="x", description="", status="pending",
             owner=None, blockedBy=[])
    s.save_task("p", t)
    fp = tmp_path / "state" / "p" / "tasks" / "t1.json"
    original = fp.read_text(encoding="utf-8")

    monkeypatch.setattr("mini_cc.storage.fs.os.replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        s.save_task("p", t)
    assert fp.read_text(encoding="utf-8") == original


# ── A2: corruption detection ───────────────────────────────────────────────

def test_load_messages_raises_on_corruption(tmp_path):
    s = FSStorage(tmp_path / "state")
    fp = tmp_path / "state" / "p" / "messages" / "s.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("{not json", encoding="utf-8")
    with pytest.raises(StorageCorruptionError):
        s.load_messages("p", "s")


def test_load_todos_raises_on_corruption(tmp_path):
    s = FSStorage(tmp_path / "state")
    fp = tmp_path / "state" / "p" / "todos" / "s.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("broken{", encoding="utf-8")
    with pytest.raises(StorageCorruptionError):
        s.load_todos("p", "s")


def test_load_messages_missing_returns_empty(tmp_path):
    """No file → empty list, not an exception."""
    s = FSStorage(tmp_path / "state")
    assert s.load_messages("p", "never") == []


def test_load_tasks_skips_corrupt_files(tmp_path):
    """Tasks are listed-best-effort: a corrupt file is logged + skipped,
    not raised, so one bad file doesn't break the whole listing."""
    s = FSStorage(tmp_path / "state")
    d = tmp_path / "state" / "p" / "tasks"
    d.mkdir(parents=True)
    (d / "good.json").write_text(json.dumps({
        "id": "good", "subject": "x", "description": "",
        "status": "pending", "owner": None, "blockedBy": []
    }))
    (d / "bad.json").write_text("not json{")
    loaded = s.load_tasks("p")
    assert len(loaded) == 1
    assert loaded[0].id == "good"


# ── A3: MCP disconnect closes clients ──────────────────────────────────────

def test_mcp_disconnect_calls_client_close():
    pool = MCPPool("p1")
    closed = {"calls": 0}

    class FakeClient:
        def __init__(self):
            self.tools = []
            self.name = "fake"

        def close(self):
            closed["calls"] += 1

    pool._clients["fake"] = FakeClient()
    assert pool.disconnect("fake") is True
    assert closed["calls"] == 1
    assert "fake" not in pool._clients


def test_mcp_disconnect_all_closes_every_client():
    pool = MCPPool("p1")
    closed = []

    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.tools = []

        def close(self):
            closed.append(self.name)

    pool._clients["a"] = FakeClient("a")
    pool._clients["b"] = FakeClient("b")
    pool.disconnect_all()
    assert sorted(closed) == ["a", "b"]
    assert pool._clients == {}


def test_mcp_disconnect_in_process_client_no_close_doesnt_explode():
    """In-process MCPClient (teaching variant) has no close() —
    disconnect must tolerate the missing attribute."""
    pool = MCPPool("p1")
    pool._clients["bare"] = MCPClient("bare")
    assert pool.disconnect("bare") is True
    assert "bare" not in pool._clients


# ── A4: background subprocess kill ─────────────────────────────────────────

def _build_ctx(tmp_path):
    """Minimal ToolContext with a real SubprocessSandbox."""
    sandbox = SubprocessSandbox(
        project_id="p1",
        project_root=tmp_path,
        policy=Policy(),
    )
    bg = BackgroundScheduler()
    return ToolContext(
        project_id="p1", session_id="s1",
        sandbox=sandbox, storage=FSStorage(tmp_path / "state"),
        todos=[], background_scheduler=bg,
        background_tools={"bash": BASH_TOOL},
    ), bg


def test_background_stop_kills_long_running_subprocess(tmp_path):
    """A bash sleep 30 backgrounded must die within seconds of stop()."""
    ctx, bg = _build_ctx(tmp_path)
    bg_id = bg.start_bg(
        ctx, "bash",
        {"command": "sleep 30", "timeout": 60},
        tool_use_id="tu_test",
        command_str="sleep 30",
    )
    # Give the worker a moment to enter sandbox.execute()
    time.sleep(0.5)
    assert bg.status(bg_id) == "running"

    t0 = time.monotonic()
    result = bg.stop(bg_id, timeout=8)
    elapsed = time.monotonic() - t0

    assert "stop requested" in result
    # Should have stopped within seconds; if it took the full timeout
    # we didn't actually kill anything.
    assert elapsed < 6.0, (
        f"stop took {elapsed:.1f}s — subprocess kill likely failed")
    # After stop, the task is marked stopped — not running.
    assert bg.status(bg_id) == "stopped"


def test_cancel_event_terminates_popen_immediately():
    """Direct sandbox test: setting cancel_event must kill the Popen."""
    import subprocess
    sandbox = SubprocessSandbox(
        project_id="p1", project_root=os.getcwd(), policy=Policy())
    ev = threading.Event()

    # Start a 30s sleep in a thread, then set the event shortly after.
    cp_holder = {}
    err_holder = []

    def runner():
        try:
            cp_holder["cp"] = sandbox.execute(
                "sleep 30", timeout=60, cancel_event=ev)
        except Exception as e:
            err_holder.append(e)

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.5)
    ev.set()
    t.join(timeout=5)

    assert not err_holder, f"unexpected exception: {err_holder}"
    cp = cp_holder["cp"]
    assert "cancelled" in cp.stdout
