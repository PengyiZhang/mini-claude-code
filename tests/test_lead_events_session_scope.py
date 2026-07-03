"""Regression tests for debug.9.md lead-events contamination bugs.

Two related symptoms pointed at the same root cause: lead session's
SSE stream got polluted by events from OTHER sessions, but the
persisted transcript stayed clean ("refresh fixes it" was the
tell-tale signal).

Root cause: TeammateSpawner._lead_events is a project-global deque,
but _lead_session_id is per-session. Stale events from session A's
lifetime leaked into session B's live stream because drain_lead_events
didn't check which session was now bound.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mini_cc.core.system_prompt import assemble_system_prompt
from mini_cc.teams import TeammateSpawner


def _make_spawner(tmp_path: Path) -> TeammateSpawner:
    return TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)


def test_set_lead_session_clears_stale_lead_events_on_switch(tmp_path):
    """drain_lead_events must not surface events from a previous lead
    session into a new session's SSE stream.

    Regression for the cross-session "Beijing weather report in a hi
    session" bug from debug.9.md."""
    spawner = _make_spawner(tmp_path)
    spawner.set_lead_session("sess-A")
    # Alice replies while A is bound — events get queued in the deque.
    spawner.bus.send("alice", "lead", "old reply from session A",
                     msg_type="result")
    spawner.bus.send("alice", "lead", "another A reply",
                     msg_type="result")
    assert len(spawner._lead_events) == 2  # control: events did queue

    # User opens a NEW session and starts /send there.
    spawner.set_lead_session("sess-B")

    # The deque must be cleared on the session switch — B's stream
    # must not see A's stale events.
    assert spawner.drain_lead_events() == []


def test_set_lead_session_idempotent_for_same_session(tmp_path):
    """Repeated /send in the same session must still drain correctly —
    the clear-on-switch path must NOT nuke same-session events."""
    spawner = _make_spawner(tmp_path)
    spawner.set_lead_session("sess-A")
    spawner.bus.send("alice", "lead", "hi 1", msg_type="result")
    # Re-bind same id (simulates the next /send in the same session).
    spawner.set_lead_session("sess-A")
    assert len(spawner.drain_lead_events()) == 1


def test_set_lead_session_clear_on_none(tmp_path):
    """Clearing the binding (sid=None) also drops the deque — there's
    no live stream to receive them."""
    spawner = _make_spawner(tmp_path)
    spawner.set_lead_session("sess-A")
    spawner.bus.send("alice", "lead", "pending", msg_type="result")
    spawner.set_lead_session(None)
    assert spawner.drain_lead_events() == []


def test_system_prompt_includes_explicit_date_with_year():
    """The agent must see 'Today's date: YYYY-MM-DD' as a standalone,
    emphatic line so it doesn't fall back to its training-cutoff year
    when generating time-sensitive tool calls (web search queries).

    Regression for the 'today 福田汽车股价' bug where the model
    searched with '2025年7月' instead of '2026年7月'."""
    out = assemble_system_prompt(
        project_root=Path("."),
        tools=[],
    )
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"Today's date: {today}" in out, (
        "system prompt must surface today's date as a standalone "
        "line — burying it inside an ISO timestamp lets the model "
        "default to its training-cutoff year"
    )
    # Also explicitly tell the model not to fall back.
    assert ("training-cutoff" in out.lower()
            or "do not fall back" in out.lower()), (
        "system prompt must explicitly warn against falling back to "
        "the training-cutoff year"
    )
