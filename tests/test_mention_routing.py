"""Task 2 (backend @ routing): parse `@name` mentions out of user input
and auto-deliver the message to the named teammates' mailboxes without
waiting for the LLM to call send_message.

Before this, the lead had to coax the model into calling send_message
to forward an @mention — which works ~60% of the time on strong models
and never on weaker ones. Routing at loop entry makes the contract
deterministic: if the user typed `@alice …`, alice gets a copy in her
inbox, full stop.
"""
from __future__ import annotations

import pytest

from mini_cc.teams import MessageBus
from mini_cc.teams.mentions import parse_mentions, route_mentions


# ── parse_mentions ───────────────────────────────────────────────────


def test_parse_returns_empty_for_plain_text():
    assert parse_mentions("hello world") == []


def test_parse_extracts_leading_at_name():
    assert parse_mentions("@alice please review") == ["alice"]


def test_parse_extracts_after_whitespace():
    # "hi @bob" — the @ is preceded by a space, so it's a real mention
    assert parse_mentions("hi @bob can you look") == ["bob"]


def test_parse_ignores_email_style_inline_at():
    # "foo@bar" is an email-like token, NOT a mention — the @ has no
    # leading whitespace/boundary. Without this guard every form field
    # the user pastes containing `foo@bar.com` would route to "bar".
    assert parse_mentions("contact me at foo@bar.com") == []


def test_parse_dedupes_preserving_first_occurrence_order():
    # Multiple mentions of the same name should only route once, and
    # the order should reflect first appearance (alice before bob).
    assert parse_mentions("@alice @bob @alice hey") == ["alice", "bob"]


def test_parse_supports_cjk_names():
    # Teammate names may legally contain CJK chars (we allow it in the
    # spawner). The parser must recognize them.
    assert parse_mentions("@张三 look at this") == ["张三"]


def test_parse_terminates_at_whitespace():
    # Trailing punctuation/whitespace is not part of the name.
    assert parse_mentions("@alice, fix the bug") == ["alice"]


def test_parse_ignores_bare_at():
    # A bare `@` with no name after it is just punctuation (e.g. an
    # email signature). Don't yield an empty-string mention.
    assert parse_mentions("@ ") == []
    assert parse_mentions("hello @ world") == []


# ── route_mentions ───────────────────────────────────────────────────


def test_route_mentions_lands_one_copy_in_each_named_inbox(tmp_path):
    bus = MessageBus(tmp_path)
    recipients = route_mentions(
        bus,
        from_agent="lead",
        text="@alice please review @bob too",
        original_text="@alice please review @bob too",
    )
    assert recipients == ["alice", "bob"]
    a = bus.read_inbox("alice")
    b = bus.read_inbox("bob")
    assert len(a) == 1
    assert len(b) == 1
    assert a[0]["from"] == "lead"
    assert a[0]["to"] == "alice"
    # The routed copy preserves the full original user text so the
    # teammate has the same context the lead got — not just the
    # mention token.
    assert "@alice" in a[0]["content"]
    assert "@bob" in a[0]["content"]
    assert a[0]["type"] == "mention"


def test_route_mentions_carries_mention_metadata(tmp_path):
    bus = MessageBus(tmp_path)
    route_mentions(bus, "lead", "@alice hi", "@alice hi")
    msg = bus.read_inbox("alice")[0]
    assert msg["metadata"].get("routed_from") == "lead"
    assert msg["metadata"].get("mention") is True


def test_route_mentions_skips_lead_self_mention(tmp_path):
    # A user typing `@lead` shouldn't route a copy back to themselves —
    # that would create a feedback echo on the next inbox drain.
    bus = MessageBus(tmp_path)
    recipients = route_mentions(bus, "lead", "@lead note to self",
                                "@lead note to self")
    assert recipients == []
    assert bus.read_inbox("lead") == []


def test_route_mentions_returns_empty_when_no_mentions(tmp_path):
    bus = MessageBus(tmp_path)
    recipients = route_mentions(bus, "lead", "plain message",
                                "plain message")
    assert recipients == []
    # No mailbox files created.
    assert list((tmp_path / ".mailboxes").glob("*.jsonl")) == []


def test_route_mentions_dedupes_repeats(tmp_path):
    bus = MessageBus(tmp_path)
    recipients = route_mentions(bus, "lead", "@alice @alice @alice hi",
                                "@alice @alice @alice hi")
    assert recipients == ["alice"]
    assert len(bus.read_inbox("alice")) == 1


def test_route_mentions_preserves_input_order(tmp_path):
    bus = MessageBus(tmp_path)
    recipients = route_mentions(bus, "lead", "@bob then @alice",
                                "@bob then @alice")
    # Order of delivery matches order of appearance in the user text.
    assert recipients == ["bob", "alice"]


def test_route_mentions_does_not_drain_existing_inbox(tmp_path):
    # Routing must use send(), not read_inbox() — pre-existing messages
    # in the teammate's inbox must survive intact.
    bus = MessageBus(tmp_path)
    bus.send("lead", "alice", "earlier work")  # pre-existing
    route_mentions(bus, "lead", "@alice more", "@alice more")
    msgs = bus.read_inbox("alice")
    assert len(msgs) == 2
    # The pre-existing message must still be there.
    assert any(m["content"] == "earlier work" for m in msgs)
    # And the new mention.
    assert any(m["type"] == "mention" for m in msgs)
