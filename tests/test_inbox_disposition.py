"""Task 5: per-message disposition tracking for teammate inboxes.

The TeammatesPanel needs to show whether each inbox message has been
read/acknowledged/ignored by the lead, so the user can tell at a
glance what's still pending. Pre-Task-5 the inbox was a flat list of
messages with no per-item state — once peeked, the lead had no way to
mark "I've seen this" vs "still needs action".

InboxDisposition tracks per-(agent, ts) state, persisted to a sidecar
JSON so it survives process restarts. Keys are message timestamps
(unique per send because MessageBus.send stamps time.time() to the
microsecond and we never reuse a stamp).
"""
from __future__ import annotations

import time

import pytest

from mini_cc.teams import MessageBus
from mini_cc.teams.disposition import InboxDisposition


def test_fresh_message_is_unread(tmp_path):
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "hello")
    msgs = bus.peek_inbox("alice")
    ts = msgs[0]["ts"]
    assert disp.state_for("alice", ts) == "unread"


def test_mark_read_persists_state(tmp_path):
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "hello")
    ts = bus.peek_inbox("alice")[0]["ts"]
    disp.mark_read("alice", ts)
    assert disp.state_for("alice", ts) == "read"
    # Reload from disk — state survives.
    disp2 = InboxDisposition(tmp_path / ".mailboxes")
    assert disp2.state_for("alice", ts) == "read"


def test_mark_ignored_overrides_read(tmp_path):
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "hello")
    ts = bus.peek_inbox("alice")[0]["ts"]
    disp.mark_read("alice", ts)
    disp.mark_ignored("alice", ts)
    assert disp.state_for("alice", ts) == "ignored"


def test_dispositions_for_agent_returns_all(tmp_path):
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "one")
    bus.send("lead", "alice", "two")
    msgs = bus.peek_inbox("alice")
    ts1, ts2 = msgs[0]["ts"], msgs[1]["ts"]
    disp.mark_read("alice", ts1)
    states = disp.dispositions_for("alice")
    assert states[str(ts1)] == "read"
    assert states.get(str(ts2)) is None  # still unread


def test_unknown_ts_returns_unread(tmp_path):
    disp = InboxDisposition(tmp_path / ".mailboxes")
    assert disp.state_for("alice", 12345.0) == "unread"


def test_state_persists_across_bus_drain(tmp_path):
    """The inbox file gets truncated on drain, but the disposition
    sidecar must persist — otherwise draining alice's inbox would
    make us forget which messages were already read. (The user might
    re-spawn alice or replay from a transcript.)"""
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "hello")
    ts = bus.peek_inbox("alice")[0]["ts"]
    disp.mark_read("alice", ts)
    # Drain — file goes away.
    bus.read_inbox("alice")
    # State still there.
    assert disp.state_for("alice", ts) == "read"


def test_clear_dispositions_for_agent(tmp_path):
    """When a teammate is deleted we should be able to purge its
    disposition state so the sidecar file doesn't grow unbounded."""
    bus = MessageBus(tmp_path)
    disp = InboxDisposition(tmp_path / ".mailboxes")
    bus.send("lead", "alice", "hello")
    ts = bus.peek_inbox("alice")[0]["ts"]
    disp.mark_read("alice", ts)
    disp.clear("alice")
    assert disp.state_for("alice", ts) == "unread"
