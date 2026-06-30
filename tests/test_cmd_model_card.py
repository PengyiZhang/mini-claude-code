"""Tests for /model slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_model(loop_state=None):
    reg = default_registry()
    cmd = reg.resolve("model")
    ctx = CommandContext(
        project_id="p",
        session_id="s",
        tenant_id="t",
        args="",
        project=None,
    )
    events = list(cmd.handler(ctx))
    return events


def _extract_card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def test_model_yields_card_event():
    events = _run_model()
    card = _extract_card(events)
    assert card is not None, f"no card event in /model output: {events!r}"
    assert card["id"] == "model"
    assert card["variant"] == "key_value"
    assert card["icon"] == "model"
    assert card["status"] == "ok"


def test_model_card_has_model_backend_fallback_pairs():
    events = _run_model()
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "model" in pairs
    assert "backend" in pairs
    assert "fallback" in pairs
    # The active model is the configured default; values are mono.
    model_pair = next(p for p in card["payload"]["pairs"] if p["k"] == "model")
    assert model_pair["mono"] is True


def test_model_card_includes_session_override_pair():
    """Session-level model override is exposed as its own pair so users
    can tell config-default vs per-session override apart."""
    events = _run_model()
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "session override" in pairs
