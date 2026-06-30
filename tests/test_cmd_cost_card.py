"""Tests for /cost slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_cost(project=None):
    reg = default_registry()
    cmd = reg.resolve("cost")
    ctx = CommandContext(
        project_id="p",
        session_id="s",
        tenant_id="t",
        args="",
        project=project,
    )
    return list(cmd.handler(ctx))


def _extract_card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def _make_project_with_metrics(input_tokens=1000, output_tokens=500,
                                cache_read=200, cache_create=50,
                                requests={"success": 10, "error": 2}):
    """Build a project with a metrics registry that returns a snapshot."""
    def snapshot_for_tenant(tenant):
        return {
            "counters": {
                "anthropic_tokens_total": {
                    "series": [
                        {"labels": {"kind": "input", "tenant": tenant}, "value": input_tokens},
                        {"labels": {"kind": "output", "tenant": tenant}, "value": output_tokens},
                        {"labels": {"kind": "cache_read", "tenant": tenant}, "value": cache_read},
                        {"labels": {"kind": "cache_create", "tenant": tenant}, "value": cache_create},
                    ],
                },
                "anthropic_request_total": {
                    "series": [
                        {"labels": {"status": k, "tenant": tenant}, "value": v}
                        for k, v in requests.items()
                    ],
                },
            },
        }
    metrics = SimpleNamespace(snapshot_for_tenant=snapshot_for_tenant)
    return SimpleNamespace(metrics=metrics, tenant_id="t1")


def test_cost_yields_card_event_when_metrics_present():
    events = _run_cost(_make_project_with_metrics())
    card = _extract_card(events)
    assert card is not None, f"no card event in /cost output: {events!r}"
    assert card["id"] == "cost"
    assert card["variant"] == "key_value"
    assert card["icon"] == "cost"
    assert card["status"] == "ok"


def test_cost_card_has_token_breakdown_pairs():
    events = _run_cost(_make_project_with_metrics())
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "input tokens" in pairs
    assert pairs["input tokens"] == "1,000"
    assert pairs["output tokens"] == "500"
    assert "total" in pairs
    # Total is computed and shown as a prominent pair.
    assert pairs["total"] == "1,750"


def test_cost_card_has_request_outcomes_pair():
    events = _run_cost(_make_project_with_metrics(requests={"success": 7, "error": 1}))
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "requests" in pairs
    assert "success=7" in pairs["requests"]
    assert "error=1" in pairs["requests"]


def test_cost_no_metrics_yields_text_not_card():
    """Without a metrics registry the command should emit a text marker
    (so 'no metrics configured' is distinguishable from 'zero usage')."""
    events = _run_cost(project=None)
    card = _extract_card(events)
    assert card is None
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    assert "not configured" in text.lower()
