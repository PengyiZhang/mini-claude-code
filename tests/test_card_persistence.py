"""Persistence tests for slash-command CardEvent payloads.

Card events must survive session compaction so a page reload shows the
same cards the user just saw. We persist them inside the existing
Anthropic transcript as synthetic ``tool_use{name="__card__"}`` blocks
paired with a ``tool_result{content='ok'}``. Reusing the tool-call
shape means the existing hydration path picks them up naturally and
session compaction treats them like any other tool call.
"""
from __future__ import annotations

from mini_cc.commands.cards import persist_card_event


def test_persist_card_event_appends_synthetic_tool_use_and_result():
    """A single card event becomes one assistant tool_use + one user
    tool_result, both with the same tool_use_id."""
    msgs: list[dict] = []
    card = {
        "type": "card",
        "id": "config",
        "variant": "key_value",
        "title": "Configuration",
        "icon": "config",
        "status": "ok",
        "payload": {"pairs": []},
        "actions": [],
        "emitted_at": 1_700_000_000.0,
        "revision": 1,
    }
    persist_card_event(msgs, card)

    assert len(msgs) == 2
    asst = msgs[0]
    user = msgs[1]
    assert asst["role"] == "assistant"
    assert isinstance(asst["content"], list) and len(asst["content"]) == 1
    block = asst["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "__card__"
    # The wire ``type`` field is dropped — it's metadata for the SSE
    # stream, not part of the persisted card shape.
    assert "type" not in block["input"]
    assert block["input"]["id"] == "config"
    assert block["input"]["variant"] == "key_value"

    assert user["role"] == "user"
    assert isinstance(user["content"], list) and len(user["content"]) == 1
    result = user["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == block["id"]
    assert result["content"] == "ok"


def test_persist_card_event_uses_unique_tool_use_ids():
    """Two cards back-to-back must not collide on tool_use_id (or
    hydration would pair the wrong result with the wrong card)."""
    msgs: list[dict] = []
    persist_card_event(msgs, {"id": "a", "variant": "list"})
    persist_card_event(msgs, {"id": "b", "variant": "list"})

    ids = [
        b["id"]
        for m in msgs
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("name") == "__card__"
    ]
    assert len(ids) == 2
    assert len(set(ids)) == 2
