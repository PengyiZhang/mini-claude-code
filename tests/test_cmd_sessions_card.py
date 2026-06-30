"""Tests for /sessions slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_sessions(project, session_manager, active_sid="s1"):
    reg = default_registry()
    cmd = reg.resolve("sessions")
    ctx = CommandContext(
        project_id="p",
        session_id=active_sid,
        tenant_id="t",
        args="",
        project=project,
        session_manager=session_manager,
    )
    return list(cmd.handler(ctx))


def _extract_card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def _meta(sid, in_memory=True, msgs=0, active_at="2026-06-30T12:00:00Z"):
    return SimpleNamespace(
        session_id=sid,
        created_at="2026-06-29T00:00:00Z",
        last_active_at=active_at,
        message_count=msgs,
        in_memory=in_memory,
    )


def test_sessions_yields_list_card():
    sm = SimpleNamespace(list=lambda pid: [_meta("s1"), _meta("s2", False)])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm)
    card = _extract_card(events)
    assert card is not None
    assert card["id"] == "sessions"
    assert card["variant"] == "list"
    assert card["icon"] == "sessions"
    items = card["payload"]["items"]
    assert len(items) == 2
    assert items[0]["title"] == "s1"
    assert items[1]["title"] == "s2"


def test_sessions_active_session_marked_with_ok_badge():
    sm = SimpleNamespace(list=lambda pid: [_meta("s1"), _meta("s2")])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm,
                            active_sid="s1")
    card = _extract_card(events)
    items = card["payload"]["items"]
    s1 = next(it for it in items if it["title"] == "s1")
    badge_texts = [b["text"].lower() for b in s1["badges"]]
    assert "active" in badge_texts


def test_sessions_warm_session_marked_with_warm_badge():
    """A session in memory should carry a 'warm' badge so users can spot
    which resume would be instant vs need a reload."""
    sm = SimpleNamespace(list=lambda pid: [_meta("warm", True), _meta("cold", False)])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm)
    card = _extract_card(events)
    items = {it["title"]: it for it in card["payload"]["items"]}
    warm_badges = [b["text"].lower() for b in items["warm"]["badges"]]
    cold_badges = [b["text"].lower() for b in items["cold"]["badges"]]
    assert "warm" in warm_badges
    assert "warm" not in cold_badges


def test_sessions_card_has_resume_expandable_command():
    """Clicking a row should fire /resume <sid>; that's the whole point
    of the list — find the session you want and resume it inline."""
    sm = SimpleNamespace(list=lambda pid: [_meta("s1")])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm)
    card = _extract_card(events)
    item = card["payload"]["items"][0]
    assert item["expandable_command"] == "/resume s1"


def test_sessions_card_summary_lists_count():
    sm = SimpleNamespace(list=lambda pid: [_meta("s1"), _meta("s2")])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm)
    card = _extract_card(events)
    assert card["payload"].get("summary")
    assert "2" in card["payload"]["summary"]


def test_sessions_no_sessions_yields_text_marker():
    """Empty list should still emit a friendly empty_hint card or text —
    we chose text so the marker reads naturally inline."""
    sm = SimpleNamespace(list=lambda pid: [])
    events = _run_sessions(project=SimpleNamespace(), session_manager=sm)
    card = _extract_card(events)
    assert card is None
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    assert "no sessions" in text.lower()
