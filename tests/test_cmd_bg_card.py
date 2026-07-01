"""Tests for /bg slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_bg(project, args=""):
    reg = default_registry()
    cmd = reg.resolve("bg")
    ctx = CommandContext(
        project_id="p",
        session_id="s",
        tenant_id="t",
        args=args,
        project=project,
    )
    return list(cmd.handler(ctx))


def _extract_card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def _task(bg_id="bg1", status="running", command="npm run build"):
    return {"bg_id": bg_id, "status": status, "command": command}


def test_bg_yields_list_card():
    bg = SimpleNamespace(
        list_tasks=lambda: [_task("bg1", "running"), _task("bg2", "completed")],
        stop=lambda bid: f"stopped {bid}",
    )
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    assert card is not None
    assert card["id"] == "bg"
    assert card["variant"] == "list"
    assert card["icon"] == "bg"
    items = card["payload"]["items"]
    assert len(items) == 2
    assert items[0]["title"] == "bg1"


def test_bg_running_task_has_running_badge_tone_ok():
    bg = SimpleNamespace(list_tasks=lambda: [_task("bg1", "running")],
                          stop=lambda bid: "")
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    item = card["payload"]["items"][0]
    badge = next(b for b in item["badges"] if "running" in b["text"].lower())
    assert badge["tone"] == "ok"


def test_bg_completed_task_carries_completed_badge():
    bg = SimpleNamespace(list_tasks=lambda: [_task("bg1", "completed")],
                          stop=lambda bid: "")
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    item = card["payload"]["items"][0]
    badge_texts = [b["text"].lower() for b in item["badges"]]
    assert any("completed" in t or "done" in t for t in badge_texts)


def test_bg_card_has_stop_menu_action_per_task():
    """Each task row exposes a 'stop' menu action so a single click
    cancels it without typing /bg stop <id>."""
    bg = SimpleNamespace(list_tasks=lambda: [_task("bg1", "running")],
                          stop=lambda bid: "")
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    item = card["payload"]["items"][0]
    stop_actions = [a for a in item["menu"] if "stop" in a["label"].lower()]
    assert stop_actions
    assert stop_actions[0]["command"] == "/bg stop bg1"


def test_bg_card_summary_counts_by_status():
    bg = SimpleNamespace(
        list_tasks=lambda: [
            _task("bg1", "running"),
            _task("bg2", "completed"),
            _task("bg3", "running"),
        ],
        stop=lambda bid: "",
    )
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    summary = card["payload"]["summary"]
    assert "2" in summary and "running" in summary.lower()
    assert "1" in summary and "completed" in summary.lower()


def test_bg_no_tasks_emits_empty_state_card():
    """Empty roster yields a list card with empty_hint, not text."""
    bg = SimpleNamespace(list_tasks=lambda: [], stop=lambda bid: "")
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint") is not None
    assert "no background" in card["payload"]["empty_hint"].lower()


def test_bg_no_scheduler_yields_text_marker():
    project = SimpleNamespace(background=None)
    events = _run_bg(project)
    card = _extract_card(events)
    assert card is None


def test_bg_running_tasks_set_refresh_command_for_live_updates():
    """A roster with at least one running task carries refresh_command
    so the frontend polls /bg every few seconds and replaces this card
    in place — the user sees status transitions without re-typing."""
    bg = SimpleNamespace(list_tasks=lambda: [_task("bg1", "running")],
                          stop=lambda bid: "")
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    assert card["refresh_command"] == "/bg"
    assert card["refresh_interval_ms"] > 0


def test_bg_all_stopped_no_refresh_command():
    """A static roster (everything stopped/completed) shouldn't poll —
    no live updates to deliver. Saves a request per mounted card."""
    bg = SimpleNamespace(
        list_tasks=lambda: [_task("bg1", "completed"),
                            _task("bg2", "stopped")],
        stop=lambda bid: "",
    )
    project = SimpleNamespace(background=bg)
    events = _run_bg(project)
    card = _extract_card(events)
    assert card["refresh_command"] is None
    assert card["refresh_interval_ms"] is None
