"""Task 1c (debug.7.md): @mention 后续轮次也要把 teammate 的事件回流到主 session。

Initial spawn wires on_event via ctx.on_subagent_event, but that sink
becomes stale once the spawn_teammate tool call returns. When the lead
later sends another message (e.g. via send_message on @alice), the
teammate wakes from idle and runs another turn — its events vanished
into the dead sink.

Fix: TeammateInfo holds an updatable event_sink; send_message re-binds
the sink to the current ctx.on_subagent_event before delivering the
message. The teammate's _runner reads info.event_sink live on every
event.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from mini_cc.teams import TeammateSpawner


class _ScriptedLoop:
    """Yields a done event each turn; records every user_input seen.

    Used to simulate a teammate waking repeatedly from idle as new
    inbox messages arrive.
    """
    def __init__(self):
        self.runs: list[str] = []

    def run(self, user_input):
        self.runs.append(user_input)
        yield {"type": "text", "text": f"turn {len(self.runs)}"}
        yield {"type": "done"}


def test_event_sink_persists_across_idle_wake_cycles(tmp_path):
    """After spawn, the lead can update the teammate's event sink so
    events from later idle-wake turns flow into the new sink (the main
    session's current SSE stream)."""
    loop = _ScriptedLoop()
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)

    spawn_events: list[dict] = []
    spawner.spawn("alice", "researcher", "hi",
                  on_event=lambda ev: spawn_events.append(ev))
    # Wait for turn 1 to complete (initial spawn prompt).
    deadline = time.time() + 2
    while time.time() < deadline and len(loop.runs) < 1:
        time.sleep(0.02)
    assert len(loop.runs) >= 1
    # Initial spawn events were captured.
    assert any(e.get("type") == "text" for e in spawn_events)

    # Now re-bind the sink — simulate the lead's next tool call setting
    # a fresh ctx.on_subagent_event. Old sink is replaced.
    later_events: list[dict] = []
    spawner.bind_event_sink("alice",
                            lambda ev: later_events.append(ev))
    # Trigger an idle wake by sending a message.
    spawner.bus.send("lead", "alice", "second task", "message")
    deadline = time.time() + 2
    while time.time() < deadline and len(loop.runs) < 2:
        time.sleep(0.02)
    assert len(loop.runs) >= 2
    # Give the runner a moment to finish emitting.
    deadline = time.time() + 0.5
    while time.time() < deadline and not later_events:
        time.sleep(0.02)
    assert any(e.get("type") == "text" for e in later_events), (
        "events from the wake-up turn did not reach the re-bound sink")

    spawner.request_shutdown("alice")
    deadline = time.time() + 2
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)


def test_clear_event_sink_stops_forwarding(tmp_path):
    """Clearing the sink returns to no-op forwarding (used when the
    lead turn ends without producing new events to surface)."""
    loop = _ScriptedLoop()
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    sink: list[dict] = []
    spawner.spawn("alice", "r", "go",
                  on_event=lambda ev: sink.append(ev))
    spawner.bind_event_sink("alice", None)
    spawner.bus.send("lead", "alice", "second", "message")
    deadline = time.time() + 2
    while time.time() < deadline and len(loop.runs) < 2:
        time.sleep(0.02)
    # Sink was cleared → no events from turn 2.
    turn2_text = [e for e in sink if "turn 2" in e.get("text", "")]
    assert turn2_text == []
    spawner.request_shutdown("alice")


def test_bind_event_sink_unknown_teammate_errors(tmp_path):
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    err = spawner.bind_event_sink("ghost", lambda ev: None)
    assert err is not None
    assert "not found" in err.lower()
