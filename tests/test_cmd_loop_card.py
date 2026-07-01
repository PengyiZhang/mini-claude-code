"""Tests for /loop slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_loop(project, args=""):
    reg = default_registry()
    cmd = reg.resolve("loop")
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


def _cron(job_id="j1", cron="*/5 * * * *", prompt="check the deploy",
          recurring=True, durable=False):
    return SimpleNamespace(
        job_id=job_id, cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )


def _wakeup(wakeup_id="w1", fire_at=12345.0, prompt="reindex", reason=""):
    return SimpleNamespace(
        wakeup_id=wakeup_id, fire_at=fire_at, prompt=prompt, reason=reason,
    )


def test_loop_yields_list_card_with_cron_jobs():
    sched = SimpleNamespace(list_jobs=lambda: [_cron()], cancel=lambda jid: "cancelled")
    project = SimpleNamespace(scheduler=sched, wakeups=SimpleNamespace(list=lambda: [], cancel=lambda jid: False))
    events = _run_loop(project)
    card = _extract_card(events)
    assert card is not None
    assert card["id"] == "loop"
    assert card["variant"] == "list"
    assert card["icon"] == "loop"
    items = card["payload"]["items"]
    assert items[0]["title"] == "j1"


def test_loop_card_distinguishes_recurring_and_oneshot():
    sched = SimpleNamespace(
        list_jobs=lambda: [_cron("rec", recurring=True),
                            _cron("one", recurring=False)],
        cancel=lambda jid: "",
    )
    project = SimpleNamespace(scheduler=sched,
                              wakeups=SimpleNamespace(list=lambda: [], cancel=lambda jid: False))
    events = _run_loop(project)
    card = _extract_card(events)
    items = {it["title"]: it for it in card["payload"]["items"]}
    rec_badges = [b["text"].lower() for b in items["rec"]["badges"]]
    one_badges = [b["text"].lower() for b in items["one"]["badges"]]
    assert any("recur" in t for t in rec_badges)
    assert any("one" in t for t in one_badges)


def test_loop_card_lists_wakeup_jobs_with_remaining_seconds():
    import time as _t
    sched = SimpleNamespace(list_jobs=lambda: [], cancel=lambda jid: "")
    wakeups = SimpleNamespace(
        list=lambda: [_wakeup("w1", fire_at=_t.monotonic() + 60)],
        cancel=lambda jid: False,
    )
    project = SimpleNamespace(scheduler=sched, wakeups=wakeups)
    events = _run_loop(project)
    card = _extract_card(events)
    items = card["payload"]["items"]
    assert any(it["title"] == "w1" for it in items)
    w1 = next(it for it in items if it["title"] == "w1")
    # Remaining seconds should appear in meta or subtitle.
    blob = (w1.get("meta") or "") + (w1.get("subtitle") or "")
    assert "s" in blob


def test_loop_card_has_cancel_menu_action_per_job():
    sched = SimpleNamespace(list_jobs=lambda: [_cron("j1")],
                              cancel=lambda jid: "")
    project = SimpleNamespace(scheduler=sched,
                              wakeups=SimpleNamespace(list=lambda: [], cancel=lambda jid: False))
    events = _run_loop(project)
    card = _extract_card(events)
    item = card["payload"]["items"][0]
    cancel_actions = [a for a in item["menu"] if "cancel" in a["label"].lower()]
    assert cancel_actions
    assert cancel_actions[0]["command"] == "/loop cancel j1"


def test_loop_no_jobs_yields_empty_state_card():
    sched = SimpleNamespace(list_jobs=lambda: [], cancel=lambda jid: "")
    project = SimpleNamespace(scheduler=sched,
                              wakeups=SimpleNamespace(list=lambda: [], cancel=lambda jid: False))
    events = _run_loop(project)
    card = _extract_card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")
    assert "no scheduled" in card["payload"]["empty_hint"].lower()
