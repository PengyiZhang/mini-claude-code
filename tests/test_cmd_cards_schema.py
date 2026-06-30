"""Schema tests for slash-command CardEvent payloads.

The card schema is the wire-format contract between Python slash-command
handlers and the React CardView renderer. Lock the shape here so a backend
rename can't silently break the frontend.
"""
from __future__ import annotations

import time

from mini_cc.commands.cards import (
    CardBadge,
    CardEvent,
    CardKeyValuePair,
    CardListPayload,
    CardListItem,
    CardAction,
    CardKeyValuePayload,
    ICON_KEYS,
    to_dict,
)


def test_card_event_serializes_to_sse_wire_format():
    ev = CardEvent(
        id="test-1",
        variant="list",
        title="T",
        payload=CardListPayload(items=[
            CardListItem(
                id="i1",
                title="Item 1",
                badges=[CardBadge(text="new", tone="ok")],
                menu=[CardAction(label="stop", command="/agents stop i1")],
            )
        ]).__dict__,
        actions=[CardAction(label="spawn", command="/agents spawn", tone="primary")],
    )
    d = to_dict(ev)
    assert d["type"] == "card"
    assert d["id"] == "test-1"
    assert d["variant"] == "list"
    assert d["title"] == "T"
    assert d["payload"]["items"][0]["title"] == "Item 1"
    assert d["payload"]["items"][0]["badges"][0]["tone"] == "ok"
    assert d["payload"]["items"][0]["menu"][0]["command"] == "/agents stop i1"
    assert d["actions"][0]["tone"] == "primary"
    assert d["emitted_at"] > 0
    assert d["revision"] == 1


def test_card_event_defaults_status_ok():
    ev = CardEvent(id="x", variant="key_value",
                   payload=CardKeyValuePayload(pairs=[]).__dict__)
    assert ev.status == "ok"
    assert to_dict(ev)["status"] == "ok"


def test_card_event_preserves_status_and_error():
    ev = CardEvent(
        id="err",
        variant="list",
        status="error",
        error_message="bus is down",
        payload={"items": []},
    )
    d = to_dict(ev)
    assert d["status"] == "error"
    assert d["error_message"] == "bus is down"


def test_key_value_payload_serializes_sensitive_flag():
    ev = CardEvent(
        id="config",
        variant="key_value",
        payload=CardKeyValuePayload(pairs=[
            CardKeyValuePair(k="API key", v="mck_secret", mono=True, sensitive=True),
        ]).__dict__,
    )
    d = to_dict(ev)
    pair = d["payload"]["pairs"][0]
    assert pair["sensitive"] is True
    assert pair["mono"] is True


def test_icon_keys_form_a_closed_enum():
    # Frontend CardIcon component maps each of these to a glyph. Adding
    # a new icon requires coordinated backend + frontend changes, so we
    # pin the set here.
    assert "agents" in ICON_KEYS
    assert "config" in ICON_KEYS
    assert "nonexistent_icon" not in ICON_KEYS


def test_to_dict_is_idempotent_within_emitted_at_tolerance():
    # emitted_at is stamped on first to_dict call; calling again should
    # not reset it to 0 (which would break SSE ordering on the client).
    ev = CardEvent(id="x", variant="list", payload={"items": []})
    first = to_dict(ev)["emitted_at"]
    second = to_dict(ev)["emitted_at"]
    assert first == second
    assert first > 0
