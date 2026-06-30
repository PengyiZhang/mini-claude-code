"""Tests for /config slash command emitting a CardEvent."""
from __future__ import annotations

from mini_cc.commands import default_registry
from mini_cc.commands.cards import to_dict, CardEvent, CardKeyValuePair, CardKeyValuePayload
from mini_cc.commands.registry import CommandContext


def _run_config(ctx_args: str = "") -> list[dict]:
    reg = default_registry()
    cmd = reg.resolve("config")
    ctx = CommandContext(
        project_id="p",
        session_id="s",
        tenant_id="t",
        args=ctx_args,
        project=None,
    )
    return list(cmd.handler(ctx))


def _extract_card(events: list[dict]) -> dict | None:
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def test_config_yields_card_event():
    events = _run_config()
    card = _extract_card(events)
    assert card is not None, f"no card event in /config output: {events!r}"
    assert card["id"] == "config"
    assert card["variant"] == "key_value"
    assert card["icon"] == "config"
    assert card["status"] == "ok"


def test_config_card_has_expected_keys():
    events = _run_config()
    card = _extract_card(events)
    assert card is not None
    keys = {p["k"] for p in card["payload"]["pairs"]}
    # The essential debug surface — model + base_url + the keys a user
    # typically needs to verify "is my proxy wired up."
    assert "primary_model" in keys
    assert "active backend" in keys
    assert "anthropic_base_url" in keys


def test_config_card_marks_api_keys_sensitive():
    """API keys must be flagged sensitive=True so the frontend masks
    them by default. Regression for leaking secrets into the visible
    chat transcript."""
    events = _run_config()
    card = _extract_card(events)
    assert card is not None
    secret_pairs = [p for p in card["payload"]["pairs"] if p["sensitive"]]
    # We expose 3 keys today (anthropic / litellm / tavily); any of
    # them being non-empty should be flagged. The test pins "at least
    # the keys that exist are flagged", not "exactly N", so adding a
    # 4th key later doesn't break the test.
    sensitive_labels = {p["k"] for p in secret_pairs}
    for p in card["payload"]["pairs"]:
        if "api_key" in p["k"] and p["v"] not in ("—", ""):
            assert p["k"] in sensitive_labels, (
                f"non-empty API key {p['k']!r} must be sensitive"
            )


def test_config_card_emitted_via_to_dict_shape():
    """The card event should match what to_dict(CardEvent(...)) would
    produce — guards against drift if someone hand-rolls the dict."""
    events = _run_config()
    card = _extract_card(events)
    assert card is not None
    # Required wire fields
    for k in ("type", "id", "variant", "status", "payload",
              "actions", "emitted_at", "revision"):
        assert k in card, f"missing wire field {k!r}"
    assert card["type"] == "card"
    assert isinstance(card["emitted_at"], (int, float))
    assert card["emitted_at"] > 0
    assert card["revision"] == 1
