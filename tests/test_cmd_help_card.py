"""Tests for /help slash command emitting a CardEvent."""
from __future__ import annotations

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_help():
    reg = default_registry()
    cmd = reg.resolve("help")
    ctx = CommandContext(project_id="p", session_id="s", tenant_id="t",
                         args="", project=None)
    return list(cmd.handler(ctx))


def _card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def test_help_yields_list_card():
    events = _run_help()
    card = _card(events)
    assert card is not None
    assert card["id"] == "help"
    assert card["variant"] == "list"
    assert card["icon"] == "help"


def test_help_card_includes_known_commands():
    events = _run_help()
    card = _card(events)
    titles = {it["title"] for it in card["payload"]["items"]}
    # A few commands we know exist regardless of plugin load order.
    assert any(t in titles for t in ("/help", "/?"))
    assert any("config" in t for t in titles)


def test_help_card_each_item_has_subtitle():
    events = _run_help()
    card = _card(events)
    for it in card["payload"]["items"]:
        # Subtitle may be empty string for edge commands but the field
        # must exist (so the renderer can always render a row).
        assert "subtitle" in it


def test_help_card_summary_counts_commands():
    events = _run_help()
    card = _card(events)
    summary = card["payload"]["summary"] or ""
    items_count = len(card["payload"]["items"])
    assert str(items_count) in summary


def test_help_command_with_aliases_has_alias_badge():
    """Commands that have aliases surface them as row badges so the
    user can see '/?' works just like '/help' at a glance."""
    events = _run_help()
    card = _card(events)
    # Find a row that should have aliases — /help itself has alias '?'.
    help_row = next(it for it in card["payload"]["items"]
                    if "help" in it["title"])
    badge_texts = [b["text"].lower() for b in help_row["badges"]]
    assert any("?" in t or "alias" in t for t in badge_texts)
