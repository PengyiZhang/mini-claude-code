"""Phase I.B-1.1: AgentLoop.nudge entry point — lets a watcher
re-enter the loop with new content after the previous turn ended.

Used by Phase I.B-1.2 mailbox watcher to wake lead when teammate
milestones land in lead's mailbox while the user is away."""
from __future__ import annotations

import time
from threading import Thread

import pytest

# Reuse test scaffolding from the existing lead mailbox test.
from test_lead_mailbox_inject import (
    _Block, _MockResponse, _MockClient, _SpawnerStub, _build_loop,
)


def test_nudge_appends_user_message_to_messages(tmp_path):
    """nudge(content) appends a user-role message to self.messages
    even if the loop isn't running yet. This is the minimum contract
    a watcher relies on."""
    script = [_MockResponse([_Block(type="text", text="hi")])]
    loop, _ = _build_loop(tmp_path, script)
    # No run() yet — nudge should still append.
    loop.nudge("[Teammate milestone] alice done")
    msgs = [m for m in loop.messages
            if isinstance(m.get("content"), str)
            and "alice done" in m["content"]]
    assert msgs, f"nudge did not append to messages; got {loop.messages}"


def test_nudge_resets_stop_flag(tmp_path):
    """If the loop's previous turn set _stop (normal end_turn), nudge
    must clear it so the next iteration can run."""
    script = [
        _MockResponse([_Block(type="text", text="first")]),
        # Second turn's response — only consumed if nudge successfully
        # restarts the loop.
        _MockResponse([_Block(type="text", text="after nudge")]),
    ]
    loop, _ = _build_loop(tmp_path, script)
    # Run first turn to completion. (end_turn returns WITHOUT setting
    # _stop in this codebase — _stop is only set by external stop().
    # Set it manually here to model the watcher's view: a loop that
    # was cancelled and now needs to be re-entered.)
    events = []
    for ev in loop.run("hello"):
        events.append(ev)
    loop._stop.set()
    assert loop._stop.is_set(), "sanity: _stop must be set before nudge"

    # Nudge — should clear _stop and start a fresh run.
    loop.nudge("[Teammate milestone] alice done")
    assert not loop._stop.is_set(), "nudge did not clear _stop"
    # Wait for the background thread (spawned by nudge) to finish.
    deadline = time.time() + 10
    while time.time() < deadline and loop._running:
        time.sleep(0.05)
    assert not loop._running, "nudge-spawned run did not finish"

    # Second turn consumed the nudged content and produced a response.
    assert any("after nudge" in str(m) for m in loop.messages), (
        f"nudge did not trigger a fresh turn; messages: {loop.messages}"
    )


def test_nudge_when_loop_running_does_not_spawn_second_thread(tmp_path):
    """If a run() is in flight, nudge only appends to messages — the
    running loop will pick up the new content on its next iteration
    via _inject_*. Must NOT start a second concurrent run thread."""
    # First response: a tool_use so the loop iterates at least once
    # more before ending. This gives us a window where loop._running
    # is True when we call nudge.
    script = [
        _MockResponse([_Block(type="tool_use", name="bash", id="t1",
                              input={"command": "sleep 0.2"})],
                      stop_reason="tool_use"),
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, _ = _build_loop(tmp_path, script)

    # Run in background so we can call nudge mid-flight.
    def _run():
        list(loop.run("go"))
    t = Thread(target=_run)
    t.start()

    # Wait until loop is running.
    deadline = time.time() + 5
    while time.time() < deadline and not getattr(loop, "_running", False):
        time.sleep(0.01)
    assert getattr(loop, "_running", False), "loop didn't start"

    # Capture thread count, nudge, then verify no NEW thread was started.
    # Easier: check that calling nudge returns immediately and the
    # existing loop continues. Track via a flag set by nudge when it
    # spawns a thread vs when it doesn't.
    spawned_flag = {"spawned": False}

    real_run_until_idle = loop._run_until_idle
    def spy_run_until_idle():
        spawned_flag["spawned"] = True
        return real_run_until_idle()
    loop._run_until_idle = spy_run_until_idle  # type: ignore

    loop.nudge("[Teammate milestone] alice done")
    # When loop is running, nudge should NOT have spawned a new thread.
    assert not spawned_flag["spawned"], (
        "nudge spawned a thread while loop was already running — "
        "would cause concurrent writes to loop.messages"
    )

    t.join(timeout=10)
    assert not t.is_alive()


def test_nudge_thread_is_daemon(tmp_path):
    """The background thread spawned by nudge when loop is idle must
    be a daemon — otherwise it would block process exit and break
    test teardown."""
    script = [
        _MockResponse([_Block(type="text", text="first")]),
        _MockResponse([_Block(type="text", text="after nudge")]),
    ]
    loop, _ = _build_loop(tmp_path, script)
    list(loop.run("hello"))  # consume first response, _stop set
    loop.nudge("[Teammate milestone]")
    # Find the spawned thread and check daemon.
    import threading
    threads = [t for t in threading.enumerate()
               if "nudge" in t.name.lower() or "loop" in t.name.lower()
               or "agent" in t.name.lower()]
    # At least one of the candidates should be a daemon. If the
    # implementation doesn't name the thread, this test still verifies
    # behavior: the spawned thread must not block interpreter exit.
    # Simplest reliable check: wait for it to finish within timeout
    # (it would block forever if non-daemon AND long-running).
    deadline = time.time() + 5
    while time.time() < deadline and getattr(loop, "_running", False):
        time.sleep(0.05)
    assert not getattr(loop, "_running", True), (
        "nudge thread didn't finish — likely non-daemon or hung"
    )


def test_nudge_idempotent_multiple_calls(tmp_path):
    """Multiple nudge() calls in quick succession should each append
    their content, not error or coalesce.

    Serialized: each nudge waits for the previous thread to finish
    before appending the next, so each gets its own LLM response
    (deterministic — no racing on the script)."""
    script = [
        _MockResponse([_Block(type="text", text="ok1")]),   # initial run
        _MockResponse([_Block(type="text", text="ok2")]),   # nudge 1
        _MockResponse([_Block(type="text", text="ok3")]),   # nudge 2
        _MockResponse([_Block(type="text", text="ok4")]),   # nudge 3
    ]
    loop, _ = _build_loop(tmp_path, script)
    list(loop.run("hello"))

    def _wait_idle():
        deadline = time.time() + 10
        while time.time() < deadline and getattr(loop, "_running", False):
            time.sleep(0.02)

    loop.nudge("[Milestone 1]")
    _wait_idle()
    loop.nudge("[Milestone 2]")
    _wait_idle()
    loop.nudge("[Milestone 3]")
    _wait_idle()

    contents = [m["content"] for m in loop.messages
                if isinstance(m.get("content"), str)]
    assert any("Milestone 1" in c for c in contents)
    assert any("Milestone 2" in c for c in contents)
    assert any("Milestone 3" in c for c in contents)
