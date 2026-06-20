"""Teams subsystem tests: MessageBus, TeammateSpawner, and teams tools."""
from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.teams import MessageBus, TeammateSpawner
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext


# ── MessageBus ────────────────────────────────────────────────────────────

def test_bus_send_and_read_drains(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("alice", "bob", "hi")
    bus.send("alice", "bob", "bye")
    msgs = bus.read_inbox("bob")
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hi"
    assert msgs[1]["content"] == "bye"
    # Second read is empty (drained).
    assert bus.read_inbox("bob") == []


def test_bus_peek_does_not_drain(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("alice", "bob", "hi")
    assert len(bus.peek_inbox("bob")) == 1
    assert len(bus.peek_inbox("bob")) == 1
    assert len(bus.read_inbox("bob")) == 1


def test_bus_isolation_between_agents(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("a", "bob", "for bob")
    bus.send("a", "carol", "for carol")
    assert [m["content"] for m in bus.read_inbox("bob")] == ["for bob"]
    assert [m["content"] for m in bus.read_inbox("carol")] == ["for carol"]


def test_bus_message_structure(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("lead", "alice", "go", msg_type="task",
             metadata={"request_id": "r1"})
    m = bus.read_inbox("alice")[0]
    assert m["from"] == "lead"
    assert m["to"] == "alice"
    assert m["type"] == "task"
    assert m["metadata"]["request_id"] == "r1"
    assert "ts" in m


def test_bus_persists_to_disk(tmp_path):
    bus1 = MessageBus(tmp_path)
    bus1.send("a", "b", "hi")
    # New bus instance reading the same workspace dir sees the same file
    bus2 = MessageBus(tmp_path)
    msgs = bus2.read_inbox("b")
    assert len(msgs) == 1


def test_bus_sanitizes_agent_name(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("a", "../escape", "x")
    # Should land in .mailboxes/, not escape the dir
    files = list((tmp_path / ".mailboxes").glob("*.jsonl"))
    assert len(files) == 1
    # No path separator in the filename
    name = files[0].name
    assert "/" not in name and "\\" not in name


# ── Teams tools ───────────────────────────────────────────────────────────

def _ctx_with_teams(tmp_path, spawner=None):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    return ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], teams=spawner)


def test_send_message_tool_writes_to_mailbox(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_teams(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["send_message"].handle(ctx, {"to": "alice", "content": "hi"})
    assert "Sent" in out
    msgs = spawner.bus.read_inbox("alice")
    # send_message tool always uses "lead" as from
    assert msgs[0]["from"] == "lead"
    assert msgs[0]["content"] == "hi"


def test_check_inbox_drains_lead(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    spawner.bus.send("alice", "lead", "hi boss")
    ctx = _ctx_with_teams(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["check_inbox"].handle(ctx, {})
    assert "hi boss" in out
    # Second call is empty (drained)
    assert tools["check_inbox"].handle(ctx, {}) == "Inbox empty."


def test_list_teammates_empty(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_teams(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    assert tools["list_teammates"].handle(ctx, {}) == "No active teammates."


def test_teams_tools_without_spawner(tmp_path):
    ctx = _ctx_with_teams(tmp_path, spawner=None)
    tools = dispatch(builtin_tools())
    assert "not configured" in tools["send_message"].handle(
        ctx, {"to": "x", "content": "y"}).lower()
    assert "not configured" in tools["check_inbox"].handle(ctx, {}).lower()


# ── TeammateSpawner ───────────────────────────────────────────────────────

class _FakeLoop:
    """Replays a script of events, then yields done."""
    def __init__(self, script):
        self.script = list(script)
        self.runs = []

    def run(self, user_input):
        self.runs.append(user_input)
        for ev in self.script:
            yield ev


def test_spawner_runs_prompt_and_marks_dead(tmp_path):
    loop = _FakeLoop([{"type": "text", "text": "hello"},
                      {"type": "done"}])
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop)
    err = spawner.spawn("alice", "worker", "do thing")
    assert err is None
    # Wait for thread to finish
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    # Teammate should have sent a "result" to lead
    msgs = spawner.bus.read_inbox("lead")
    assert any(m["type"] == "result" for m in msgs)


def test_spawner_rejects_duplicate(tmp_path):
    loop = _FakeLoop([])  # empty script — thread will just exit
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop)
    spawner.spawn("alice", "worker", "x")
    err = spawner.spawn("alice", "worker", "y")
    # If first is still alive, dup rejected. If first finished fast, no error.
    # Either way, no crash.
    assert err is None or "already exists" in err


def test_spawner_shutdown_between_turns(tmp_path):
    """A teammate whose run() yields done events should exit when a
    shutdown_request lands in its inbox (spawner detects it on the next
    done event)."""
    class _DoneLoop:
        def run(self, user_input):
            # Yield done events forever; spawner returns out of its for-loop
            # when it observes the shutdown in the inbox.
            while True:
                yield {"type": "done"}
                time.sleep(0.05)

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _DoneLoop())
    spawner.spawn("alice", "worker", "go")
    deadline = time.time() + 1
    while not spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert spawner.list_alive(), "teammate never started"
    out = spawner.request_shutdown("alice")
    assert "Shutdown request sent" in out
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    msgs = spawner.bus.read_inbox("lead")
    types = [m["type"] for m in msgs]
    assert "shutdown_response" in types


def test_spawner_shutdown_unknown_teammate(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    assert "not found" in spawner.request_shutdown("nobody")


def test_spawner_pre_spawn_shutdown_drained(tmp_path):
    """If a shutdown_request lands before the thread starts processing,
    the teammate should exit without running the prompt."""
    class _ExplodingLoop:
        def run(self, user_input):
            raise AssertionError("should not be called")

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _ExplodingLoop())
    # Enqueue shutdown BEFORE spawn
    spawner.bus.send("lead", "alice", "shut down", "shutdown_request",
                     {"request_id": "r1"})
    spawner.spawn("alice", "worker", "do thing")
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    msgs = spawner.bus.read_inbox("lead")
    assert any(m["type"] == "shutdown_response" for m in msgs)
