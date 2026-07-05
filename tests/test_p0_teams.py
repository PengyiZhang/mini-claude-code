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
    # Should land in .mailboxes/, not escape the dir. Both the live
    # inbox and the append-only history land inside .mailboxes/.
    files = list((tmp_path / ".mailboxes").glob("*.jsonl"))
    assert len(files) == 2
    # No path separator in any filename (path-traversal blocked)
    for f in files:
        assert "/" not in f.name and "\\" not in f.name
    # All inside .mailboxes/
    assert all(f.parent == (tmp_path / ".mailboxes") for f in files)


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
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    err = spawner.spawn("alice", "worker", "do thing", persistent=False)
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
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    spawner.spawn("alice", "worker", "x")
    # Wait for first to finish so dup is unambiguous.
    deadline = time.time() + 2
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)
    err = spawner.spawn("alice", "worker", "y")
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


# ── ProtocolTracker ───────────────────────────────────────────────────────

def test_protocol_tracker_register_get_update():
    from mini_cc.teams import ProtocolState, ProtocolTracker
    t = ProtocolTracker()
    rid = t.new_request_id()
    assert rid.startswith("req_")
    s = ProtocolState(request_id=rid, type="plan_approval",
                     sender="alice", target="lead",
                     status="pending", payload="do x")
    t.register(s)
    assert t.get(rid) is s
    assert t.update_status(rid, "approved")
    assert t.get(rid).status == "approved"
    assert not t.update_status("missing", "approved")


def test_protocol_tracker_list_pending_filters_status():
    from mini_cc.teams import ProtocolState, ProtocolTracker
    t = ProtocolTracker()
    s1 = ProtocolState(request_id="r1", type="plan_approval",
                      sender="a", target="lead", status="pending", payload="")
    s2 = ProtocolState(request_id="r2", type="plan_approval",
                      sender="b", target="lead", status="approved", payload="")
    t.register(s1)
    t.register(s2)
    pending = t.list_pending()
    assert [s.request_id for s in pending] == ["r1"]


# ── Plan-approval tools ──────────────────────────────────────────────────

def _ctx_with_session(tmp_path, spawner, session_id="s1"):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    return ToolContext(project_id="p", session_id=session_id,
                      sandbox=sandbox, storage=storage, todos=[],
                      teams=spawner)


def test_submit_plan_tool_rejects_non_teammate(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner, session_id="lead-sess")
    tools = dispatch(builtin_tools())
    out = tools["submit_plan"].handle(ctx, {"plan": "do thing"})
    assert "only available to teammates" in out


def test_spawn_teammate_rejects_calls_from_teammate_session(tmp_path):
    """P0-7: a teammate session_id must not be able to spawn its own
    teammates — that would fan out unboundedly (each spawned teammate
    gets the same tool set and could recurse again)."""
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner,
                            session_id="teammate-alice")
    tools = dispatch(builtin_tools())
    out = tools["spawn_teammate"].handle(
        ctx, {"name": "eve", "prompt": "do evil"})
    assert "recursion cap" in out.lower()
    # And no thread was started.
    assert not spawner.list_alive()


def test_spawn_teammate_allows_lead_session(tmp_path):
    """Regression guard: the recursion cap must NOT block the lead from
    spawning — only teammate-originated calls are refused."""
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner, session_id="lead-sess")
    tools = dispatch(builtin_tools())
    out = tools["spawn_teammate"].handle(
        ctx, {"name": "alice", "prompt": "do work"})
    assert "spawned" in out


def test_submit_plan_tool_registers_and_notifies_lead(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner,
                            session_id="teammate-alice")
    tools = dispatch(builtin_tools())
    out = tools["submit_plan"].handle(ctx, {"plan": "my plan"})
    assert "Plan submitted" in out
    req_id = out.split("(")[1].split(")")[0]
    # Lead inbox has the plan_approval_request
    lead_msgs = spawner.bus.read_inbox("lead")
    assert len(lead_msgs) == 1
    assert lead_msgs[0]["type"] == "plan_approval_request"
    assert lead_msgs[0]["metadata"]["request_id"] == req_id
    # Tracker has it pending
    states = spawner.protocol.list_pending()
    assert len(states) == 1
    assert states[0].request_id == req_id
    # Spawner internal map says alice is blocked on it
    assert spawner._waiting_plan["alice"] == req_id


def test_review_plan_tool_unknown_request(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["review_plan"].handle(
        ctx, {"request_id": "req_missing", "approve": True})
    assert "not found" in out


def test_review_plan_tool_approves_and_sends_response(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    req_id = spawner.submit_plan("alice", "my plan")
    ctx = _ctx_with_session(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["review_plan"].handle(
        ctx, {"request_id": req_id, "approve": True})
    assert "approved" in out
    # alice's inbox has the response
    msgs = spawner.bus.read_inbox("alice")
    assert any(m["type"] == "plan_approval_response"
               and m["metadata"]["approve"] for m in msgs)
    # Tracker updated
    assert spawner.protocol.get(req_id).status == "approved"


def test_request_plan_tool_unknown_teammate(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx_with_session(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["request_plan"].handle(
        ctx, {"teammate": "nobody", "task": "x"})
    assert "not found" in out


def test_request_plan_tool_sends_message(tmp_path):
    """request_plan on an alive teammate sends a 'message' to its inbox."""
    # Block the teammate's loop so it stays alive while we test.
    block = threading.Event()

    class _BlockingLoop:
        def run(self, user_input):
            yield {"type": "text", "text": "starting"}
            # Hold the generator open; spawner's for-loop will block on
            # next() and not progress to idle_poll.
            while not block.is_set():
                time.sleep(0.02)
                yield {"type": "text", "text": "tick"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _BlockingLoop(),
        idle_poll_interval=0.02, idle_timeout=10)
    spawner.spawn("alice", "worker", "prime")
    # Wait until alice is alive
    deadline = time.time() + 2
    while not spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)
    try:
        ctx = _ctx_with_session(tmp_path, spawner)
        tools = dispatch(builtin_tools())
        out = tools["request_plan"].handle(
            ctx, {"teammate": "alice", "task": "build it"})
        assert "Asked alice" in out
        # alice should have a message in inbox
        msgs = spawner.bus.peek_inbox("alice")
        assert any(m["type"] == "message" and "build it" in m["content"]
                   for m in msgs)
    finally:
        # Let the teammate finish so the test can clean up.
        block.set()


# ── Plan-approval end-to-end via spawner ─────────────────────────────────

def test_spawner_plan_approval_cycle(tmp_path):
    """Full cycle: teammate runs, calls submit_plan (simulated via direct
    spawner call), spawner blocks, lead approves, teammate runs again with
    [Plan approved] as next prompt."""
    runs = []
    state = {"submitted": False}

    class _TwoRunLoop:
        def run(self, user_input):
            runs.append(user_input)
            if not state["submitted"]:
                state["submitted"] = True
                spawner.submit_plan("alice", "auto-injected plan")
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _TwoRunLoop(),
        idle_poll_interval=0.02, idle_timeout=0.1)

    spawner.spawn("alice", "worker", "initial prompt", persistent=False)
    # Wait for the first turn + submit_plan to land
    deadline = time.time() + 2
    while not spawner._waiting_plan.get("alice") and time.time() < deadline:
        time.sleep(0.02)
    req_id = spawner._waiting_plan.get("alice")
    assert req_id is not None, "teammate should be blocked on plan"
    spawner.review_plan(req_id, approve=True, feedback="go")
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    assert len(runs) == 2
    assert "[Plan approved]" in runs[1]


def test_spawner_plan_approval_rejection_carries_feedback(tmp_path):
    runs = []
    state = {"submitted": False}

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            if not state["submitted"]:
                state["submitted"] = True
                spawner.submit_plan("alice", "p")
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        idle_poll_interval=0.02, idle_timeout=0.1)

    spawner.spawn("alice", "worker", "go", persistent=False)
    deadline = time.time() + 2
    while not spawner._waiting_plan.get("alice") and time.time() < deadline:
        time.sleep(0.02)
    req_id = spawner._waiting_plan.get("alice")
    spawner.review_plan(req_id, approve=False, feedback="redo it")
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert len(runs) == 2
    assert "redo it" in runs[1]


def test_spawner_plan_approval_shutdown_during_wait(tmp_path):
    """If the lead sends shutdown_request while a teammate is blocked on
    plan approval, the teammate should exit with a shutdown_response."""
    state = {"submitted": False}

    class _Loop:
        def run(self, user_input):
            if not state["submitted"]:
                state["submitted"] = True
                spawner.submit_plan("alice", "p")
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        idle_poll_interval=0.02, idle_timeout=10)

    spawner.spawn("alice", "worker", "go", persistent=False)
    deadline = time.time() + 2
    while not spawner._waiting_plan.get("alice") and time.time() < deadline:
        time.sleep(0.02)
    assert spawner._waiting_plan.get("alice") is not None
    spawner.request_shutdown("alice")
    deadline = time.time() + 3
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    msgs = spawner.bus.read_inbox("lead")
    assert any(m["type"] == "shutdown_response" for m in msgs)


# ── Idle poll + auto-claim ───────────────────────────────────────────────

def _storage_with_pending_task(tmp_path, subject="unclaimed",
                               deps=None, worktree=""):
    from mini_cc.storage import Task
    storage = FSStorage(tmp_path / "state")
    storage.save_task("p", Task(
        id="task_pending1",
        subject=subject,
        description="",
        status="pending",
        owner=None,
        blockedBy=deps or [],
        worktree=worktree,
    ))
    return storage


def test_idle_poll_claims_unclaimed_task(tmp_path):
    runs = []
    storage = _storage_with_pending_task(tmp_path)

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p", storage=storage,
        idle_poll_interval=0.02, idle_timeout=2)
    spawner.spawn("alice", "worker", "first", persistent=False)
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    # First run: initial prompt. Second run: auto-claimed task.
    assert len(runs) == 2
    assert "<auto-claimed>" in runs[1]
    assert "task_pending1" in runs[1]
    # Task is now owned by alice and in_progress
    tasks = storage.load_tasks("p")
    assert tasks[0].owner == "alice"
    assert tasks[0].status == "in_progress"


def test_idle_poll_skips_blocked_task(tmp_path):
    """A task whose dep isn't completed should not be auto-claimed.
    Teammate should hit idle timeout and exit without a second run."""
    runs = []
    storage = _storage_with_pending_task(tmp_path,
                                         deps=["task_blocker"])

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p", storage=storage,
        idle_poll_interval=0.02, idle_timeout=0.3)
    spawner.spawn("alice", "worker", "first", persistent=False)
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    # Only the initial run happened
    assert len(runs) == 1
    tasks = storage.load_tasks("p")
    assert tasks[0].owner is None


def test_idle_poll_timeout_exits_teammate(tmp_path):
    runs = []

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p",
        storage=FSStorage(tmp_path / "state"),
        idle_poll_interval=0.02, idle_timeout=0.2)
    spawner.spawn("alice", "worker", "only turn", persistent=False)
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    assert len(runs) == 1  # never got second work


def test_idle_poll_inbox_message_becomes_next_prompt(tmp_path):
    runs = []

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        idle_poll_interval=0.02, idle_timeout=1)
    spawner.spawn("alice", "worker", "first", persistent=False)
    # Give the first turn time to complete, then send a message
    time.sleep(0.2)
    spawner.bus.send("lead", "alice", "do more work", "message")
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    assert len(runs) == 2
    assert "<inbox>" in runs[1]
    assert "do more work" in runs[1]


# ── wt_ctx auto-cwd on claim ────────────────────────────────────────────

def test_idle_poll_with_worktree_redirects_loop_cwd(tmp_path):
    """When auto-claiming a task with a worktree, the spawner must
    redirect the teammate's loop sandbox to the worktree path before
    the claimed-task turn starts (s20 wt_ctx behavior)."""
    storage = _storage_with_pending_task(tmp_path, worktree="wt1")
    wt_path = tmp_path / "ws" / ".worktrees" / "wt1"
    wt_path.mkdir(parents=True, exist_ok=True)

    set_calls: list = []

    class _Loop:
        def run(self, user_input):
            yield {"type": "done"}

        def set_worktree(self, path):
            set_calls.append(path)

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p", storage=storage,
        idle_poll_interval=0.02, idle_timeout=1)
    spawner.spawn("alice", "worker", "first", persistent=False)
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    assert set_calls == [wt_path]


def test_idle_poll_no_worktree_does_not_redirect(tmp_path):
    """A task without a worktree binding must not trigger set_worktree."""
    storage = _storage_with_pending_task(tmp_path, worktree="")

    set_calls: list = []

    class _Loop:
        def run(self, user_input):
            yield {"type": "done"}

        def set_worktree(self, path):
            set_calls.append(path)

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p", storage=storage,
        idle_poll_interval=0.02, idle_timeout=0.3)
    spawner.spawn("alice", "worker", "first", persistent=False)
    deadline = time.time() + 5
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert spawner.list_alive() == []
    assert set_calls == []


def test_agentloop_set_worktree_swaps_sandbox(tmp_path):
    """AgentLoop.set_worktree replaces its sandbox with one rooted at
    the worktree path. Subsequent tool calls resolve paths against it."""
    from mini_cc.core.loop import AgentLoop, ProjectRef
    from dataclasses import dataclass
    from typing import Iterator

    original_sandbox = SubprocessSandbox("p", tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="p", project_root=str(tmp_path / "ws"),
                     sandbox=original_sandbox, storage=storage)
    loop = AgentLoop(ref, "s1")
    wt = tmp_path / "wt1"
    wt.mkdir(parents=True, exist_ok=True)
    loop.set_worktree(wt)
    # The new sandbox resolves relative paths against wt, not ws
    assert loop.project.sandbox.project_root == wt.resolve()


def test_stopped_teammates_get_pruned_from_registry(tmp_path):
    """Stopped teammates must not accumulate in `_teammates` forever.

    Pre-fix, the runner's `finally` block marked `alive=False` but left
    the entry in `_teammates`. Across many spawn/stop cycles the
    registry grew unbounded and `/agents` listing rendered every dead
    entry forever.
    """
    class _Loop:
        def run(self, prompt, *, on_event=None):
            # Exit immediately — runner sees no inbox activity and ends.
            return "done"

    storage = FSStorage(tmp_path / "state")
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        project_id="p", storage=storage,
        idle_poll_interval=0.02, idle_timeout=0.3)

    # Spawn and let die, 5 times.
    for i in range(5):
        spawner.spawn(f"t{i}", "worker", "go")
        deadline = time.time() + 5
        while spawner.list_alive() and time.time() < deadline:
            time.sleep(0.02)

    # All stopped. After a prune, _teammates should retain at most a
    # small bounded tail of recently-stopped entries (so /agents can
    # still show "recently stopped"), not all 5.
    assert len(spawner.list_alive()) == 0
    assert len(spawner._teammates) <= 3, (
        f"registry leak: {len(spawner._teammates)} entries retained "
        f"after 5 spawn/stop cycles; expected ≤ 3")


# ── Inbox dialogue encoding ───────────────────────────────────────────────

def test_inbox_dialogue_preserves_unicode(tmp_path):
    """Regression: _format_inbox_as_dialogue must NOT JSON-escape non-ASCII
    content. The `<!-- raw: ... -->` fallback line used json.dumps with the
    default ensure_ascii=True, which wrote Chinese as literal backslash-u
    escape sequences into the teammate's transcript — visible as garbled
    "JSON ASCII" text in the chat bubble after a page refresh."""
    from mini_cc.teams import _format_inbox_as_dialogue
    bus = MessageBus(tmp_path)
    bus.send("lead", "alice", "你好，开始吧", "mention")
    inbox = bus.read_inbox("alice")
    out = _format_inbox_as_dialogue(inbox, "alice")
    # The readable line carries the Chinese content verbatim...
    assert "你好，开始吧" in out
    # ...and the raw JSON fallback must NOT have ASCII-escaped it.
    assert "\\u4f60" not in out, out
    assert "\\u597d" not in out, out


# ── Park-after-result ─────────────────────────────────────────────────────

def test_teammate_parks_after_result_and_ignores_chatter(tmp_path):
    """A teammate that sends msg_type="result" (mission complete) must
    park: it stays alive but does NOT run another LLM turn on subsequent
    plain chatter (e.g. the lead's acknowledgement), only on a genuine
    new-task signal (lead @mention) or shutdown. This is what stops the
    "keeps making meaningless LLM calls after task completion" loop."""
    runs = []
    state = {"sent_result": False}

    class _Loop:
        def run(self, user_input):
            runs.append(user_input)
            if not state["sent_result"]:
                # Simulate the teammate emitting its completion summary.
                spawner.bus.send("alice", "lead", "all done", "result")
                state["sent_result"] = True
            yield {"type": "done"}

    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: _Loop(),
        idle_poll_interval=0.02, idle_timeout=0.3)
    spawner.spawn("alice", "worker", "go", persistent=True)

    # Wait for the first turn to land and the runner to flip parked.
    deadline = time.time() + 2
    info = None
    while time.time() < deadline:
        with spawner._lock:
            info = spawner._teammates.get("alice")
        if info is not None and info.parked:
            break
        time.sleep(0.02)
    assert info is not None and info.parked, "teammate should park after result"
    assert len(runs) == 1

    # Plain chatter (lead ack) must NOT wake the parked teammate.
    spawner.bus.send("lead", "alice", "thanks, nice work", "message")
    time.sleep(0.6)  # well past idle_timeout
    assert len(runs) == 1, f"parked teammate woke on chatter: {runs}"
    assert spawner.list_alive(), "parked teammate must stay alive"

    # A lead @mention (new task) MUST wake it.
    spawner.bus.send("lead", "alice", "new task: do X", "mention")
    deadline = time.time() + 3
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(runs) >= 2, "parked teammate did not wake on mention"
    assert "new task: do X" in runs[1]

    # Clean shutdown so the test doesn't leak a thread.
    spawner.request_shutdown("alice")
    deadline = time.time() + 3
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert spawner.list_alive() == []

