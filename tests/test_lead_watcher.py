"""Phase I.B-1.2: LeadWatcher daemon — polls lead's mailbox and nudges
the lead's AgentLoop when teammate result/milestone/blocker messages
land while the user is away.

Connects Phase I.A's auto-CC mechanism (the bus copies teammate→teammate
result/milestone/blocker messages into lead's mailbox with metadata.cc=True)
to Phase I.B-1.1's AgentLoop.nudge entry point.

Drain semantics (Option C from the task spec): when the watcher fires
it DRAINS the entire lead mailbox — direct replies AND CC'd copies —
and synthesizes a single nudge that summarises every drained message.
This is consistent with how MessageBus is meant to be consumed (read_inbox
is all-or-nothing) and means the lead's own _inject_teammate_replies
path will see an empty mailbox on the next user /send (the nudge already
delivered the content).
"""
from __future__ import annotations

import time

import pytest

from mini_cc.teams import TeammateSpawner
from mini_cc.teams.watcher import LeadWatcher

# Reuse test scaffolding from the existing lead mailbox test.
from test_lead_mailbox_inject import (
    _Block, _MockResponse, _MockClient, _SpawnerStub, _build_loop,
)


def _wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll ``predicate`` until it returns truthy or timeout (in seconds).
    Returns the predicate's last value. Used to wait for the watcher's
    daemon thread to do its job without sleeping for fixed durations."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def test_watcher_nudges_lead_loop_when_cc_message_lands(tmp_path):
    """A CC'd result message lands in lead's mailbox while the user is
    away. The watcher must debounce, drain the mailbox, and nudge the
    lead loop with a synthesised summary so the lead turns over."""
    # Two scripted responses: the nudge triggers one full turn. We give
    # a second response in case the loop iterates an extra time.
    script = [
        _MockResponse([_Block(type="text", text="got teammate update")]),
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.2,
    )
    watcher.start()
    try:
        # Teammate alice reports a result; bus auto-CCs lead.
        bus.send("alice", "charlie", "feature X complete",
                 msg_type="result")
        # Wait for the nudge to drive a full turn (poll + debounce +
        # turn overhead). The lead's LLM response should land in
        # messages.
        ok = _wait_for(
            lambda: any("got teammate update" in str(m)
                        for m in loop.messages),
            timeout=8.0,
        )
        assert ok, (
            "watcher did not nudge lead loop — messages: "
            f"{loop.messages}"
        )
        # Mailbox drained.
        assert bus.peek_inbox("lead") == [], (
            "watcher should have drained lead's mailbox"
        )
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_debounces_burst_of_messages(tmp_path):
    """Three CC'd messages arrive within ~0.1s. The watcher must
    collapse them into a SINGLE nudge — one LLM call, one turn.

    Asserts on the LLM client's call count: debounce exists precisely
    to keep this at 1 (vs 3 if every message triggered its own nudge).
    """
    script = [
        _MockResponse([_Block(type="text", text="ok1")]),
        _MockResponse([_Block(type="text", text="ok2")]),
        _MockResponse([_Block(type="text", text="ok3")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.3,
    )
    watcher.start()
    try:
        for i in range(3):
            bus.send(f"alice{i}", "bob", f"update {i}",
                     msg_type="milestone")
            time.sleep(0.02)
        # Wait long enough for debounce + a single turn to land.
        ok = _wait_for(
            lambda: any("update 2" in str(m) for m in loop.messages),
            timeout=6.0,
        )
        assert ok, (
            "watcher did not deliver the burst as a nudge; messages: "
            f"{loop.messages}"
        )
        # All three updates should appear in a single nudge payload.
        nudge_msgs = [m for m in loop.messages
                      if isinstance(m.get("content"), str)
                      and "Teammate messages" in m["content"]]
        assert len(nudge_msgs) == 1, (
            "expected exactly ONE nudge after debounce, got "
            f"{len(nudge_msgs)}: {nudge_msgs}"
        )
        payload = nudge_msgs[0]["content"]
        assert "update 0" in payload
        assert "update 1" in payload
        assert "update 2" in payload
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_ignores_non_cc_messages(tmp_path):
    """A direct chat message (msg_type=message) does not match the
    watcher's trigger filter (CC'd or result/milestone/blocker). The
    watcher must NOT nudge and must NOT drain the mailbox — the lead's
    own /send path is responsible for plain messages."""
    script = [
        _MockResponse([_Block(type="text", text="should not happen")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.1,
    )
    watcher.start()
    try:
        # Direct chat from alice to lead — NOT CC'd, NOT a trigger type.
        bus.send("alice", "lead", "hey, quick question",
                 msg_type="message")
        # Observation window: longer than debounce+poll so a buggy
        # watcher would have had time to fire.
        time.sleep(0.5)
        assert loop.messages == [], (
            "watcher nudged on a non-trigger message; messages: "
            f"{loop.messages}"
        )
        # The message must still be in the mailbox (we don't drain
        # non-triggering batches).
        inbox = bus.peek_inbox("lead")
        assert len(inbox) == 1, (
            f"watcher drained a non-triggering batch; inbox: {inbox}"
        )
        assert inbox[0]["content"] == "hey, quick question"
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_skips_when_no_lead_loop(tmp_path):
    """If lead_loop_getter returns None (no lead session is active),
    the watcher must tick without crashing and without nudging."""
    script: list = []
    loop, bus = _build_loop(tmp_path, script)

    # Getter always returns None.
    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: None,
        poll_interval=0.05,
        debounce=0.1,
    )
    watcher.start()
    try:
        # Drop a CC'd message in; watcher should detect but skip the
        # nudge because no lead loop is bound.
        bus.send("alice", "bob", "done", msg_type="result")
        time.sleep(0.4)
        # Nothing in messages (loop was never reached + script empty).
        assert loop.messages == []
        # The watcher must NOT have drained the mailbox either —
        # nothing to do with the messages if no loop to nudge.
        inbox = bus.peek_inbox("lead")
        assert any(m.get("type") == "result" for m in inbox), (
            "watcher drained mailbox with no lead loop to deliver to; "
            f"inbox: {inbox}"
        )
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_stops_cleanly(tmp_path):
    """start() then immediate stop() must let join() return within 1s.
    Daemon thread should exit cleanly via the _stop event."""
    loop, bus = _build_loop(tmp_path, [])
    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.1,
    )
    watcher.start()
    t0 = time.time()
    watcher.stop(join_timeout=2.0)
    elapsed = time.time() - t0
    assert elapsed < 1.0, (
        f"watcher.stop took {elapsed:.2f}s, expected < 1s"
    )
    assert not watcher.is_alive(), (
        "watcher daemon thread still alive after stop()"
    )


def test_watcher_does_not_nudge_if_mailbox_emptied_during_debounce(tmp_path):
    """Race: a CC message lands, watcher detects it and starts the
    debounce window, but during the debounce the lead's own /send runs
    and drains the mailbox (the user came back). After debounce the
    watcher must NOT nudge — there's nothing left to deliver and the
    user is now present.

    Models the TOCTOU window between detect-and-debounce and drain.
    The watcher must re-check the mailbox is non-empty AFTER the
    debounce wait, not trust the pre-debounce snapshot.
    """
    script = [
        _MockResponse([_Block(type="text", text="should not happen")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.3,
    )
    watcher.start()
    try:
        # Drop the CC message — watcher will detect on next poll.
        bus.send("alice", "bob", "result is in", msg_type="result")
        # Give the watcher one poll cycle to detect + start debounce.
        time.sleep(0.1)
        # Simulate the user coming back: drain the mailbox out from
        # under the watcher.
        bus.read_inbox("lead")
        # Wait beyond debounce so the watcher reaches its post-debounce
        # drain attempt.
        time.sleep(0.6)
        assert loop.messages == [], (
            "watcher nudged even though mailbox was emptied mid-debounce; "
            f"messages: {loop.messages}"
        )
    finally:
        watcher.stop(join_timeout=2.0)


# ── TeammateSpawner integration ─────────────────────────────────────────


def _build_spawner(tmp_path):
    """Construct a TeammateSpawner with a stub loop_factory that is
    never actually exercised (we don't spawn teammates in these tests)."""
    def _factory(name):
        raise RuntimeError("no teammates should be spawned in this test")

    return TeammateSpawner(
        workspace=tmp_path / "ws",
        loop_factory=_factory,
        project_id="proj-x",
    )


def test_spawner_start_stop_lead_watcher(tmp_path):
    """TeammateSpawner.start_lead_watcher constructs a LeadWatcher bound
    to its bus + project_id; stop_lead_watcher shuts it down cleanly
    and clears the spawner's handle."""
    spawner = _build_spawner(tmp_path)
    spawner.start_lead_watcher(lambda: None)
    handle = spawner._lead_watcher
    assert handle is not None
    assert handle.is_alive()
    spawner.stop_lead_watcher(join_timeout=2.0)
    # stop clears the handle and the underlying thread has exited.
    assert spawner._lead_watcher is None
    assert not handle.is_alive()


def test_spawner_start_lead_watcher_idempotent(tmp_path):
    """Calling start_lead_watcher twice must not leak a second daemon
    thread — the second call is a no-op (or stops+restarts cleanly)."""
    spawner = _build_spawner(tmp_path)
    spawner.start_lead_watcher(lambda: None)
    first = spawner._lead_watcher
    spawner.start_lead_watcher(lambda: None)
    try:
        # Either the same instance or a fresh one — but no more than
        # one alive daemon thread at any time.
        import threading
        alive = [t for t in threading.enumerate()
                 if "LeadWatcher" in t.name]
        assert len(alive) <= 1, (
            f"multiple watcher threads alive: {alive}"
        )
    finally:
        spawner.stop_lead_watcher(join_timeout=2.0)
