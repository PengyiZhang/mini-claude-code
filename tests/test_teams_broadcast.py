"""debug.8 Task A: MessageBus.broadcast — one send, many inboxes.

The teams workflow needs the lead to broadcast a single message to
every alive teammate at once (e.g. "stop what you're doing and
summarize"). Pre-broadcast the lead had to call send_message in a
loop, which (a) burned N tool calls and (b) couldn't be atomic across
new arrivals — a teammate spawned between two sends would miss the
first message. broadcast() makes one pass over the current alive
teammates and drops the same message into each inbox.
"""
from __future__ import annotations

from mini_cc.teams import MessageBus


def test_broadcast_lands_in_every_named_inbox(tmp_path):
    """broadcast(to_agents=[...]) writes the same message into each listed agent."""
    bus = MessageBus(tmp_path)
    recipients = bus.broadcast(
        from_agent="lead",
        to_agents=["alice", "bob", "carol"],
        content="stand up for roll call",
        msg_type="announcement",
    )
    assert sorted(recipients) == ["alice", "bob", "carol"]
    for who in recipients:
        msgs = bus.read_inbox(who)
        assert len(msgs) == 1, f"{who} missing broadcast"
        m = msgs[0]
        assert m["from"] == "lead"
        assert m["to"] == who
        assert m["content"] == "stand up for roll call"
        assert m["type"] == "announcement"


def test_broadcast_message_marks_metadata_broadcast(tmp_path):
    """Each broadcast copy carries ``metadata.broadcast = True`` so the
    receiver can tell it was a broadcast (vs a targeted unicast) and
    decide whether to reply privately or back to the group."""
    bus = MessageBus(tmp_path)
    bus.broadcast("lead", ["alice", "bob"], "ping")
    a = bus.read_inbox("alice")[0]
    b = bus.read_inbox("bob")[0]
    assert a["metadata"].get("broadcast") is True
    assert b["metadata"].get("broadcast") is True
    # broadcast_id ties the copies together — same value on each
    assert a["metadata"]["broadcast_id"] == b["metadata"]["broadcast_id"]


def test_broadcast_with_empty_recipients_is_noop(tmp_path):
    bus = MessageBus(tmp_path)
    recipients = bus.broadcast("lead", [], "nothing to see")
    assert recipients == []
    # No mailbox files created.
    files = list((tmp_path / ".mailboxes").glob("*.jsonl"))
    assert files == []


def test_broadcast_dedupes_recipients(tmp_path):
    """Calling broadcast(["alice", "alice"], ...) drops exactly one
    copy into alice's inbox (we don't double-send on duplicates)."""
    bus = MessageBus(tmp_path)
    bus.broadcast("lead", ["alice", "alice", "alice"], "one copy")
    msgs = bus.read_inbox("alice")
    assert len(msgs) == 1


def test_broadcast_skips_self_if_listed(tmp_path):
    """broadcast's most common bug is the lead broadcasting to itself
    by accident. Default behavior: skip self silently. The caller can
    see what landed via the returned recipient list."""
    bus = MessageBus(tmp_path)
    recipients = bus.broadcast(
        "lead", ["lead", "alice", "bob"], "team huddle")
    assert sorted(recipients) == ["alice", "bob"]
    # lead's own inbox must not get the broadcast copy
    assert bus.read_inbox("lead") == []
