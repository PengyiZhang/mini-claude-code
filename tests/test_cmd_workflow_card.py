"""Tests for /workflow slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run(project, args=""):
    reg = default_registry()
    cmd = reg.resolve("workflow")
    ctx = CommandContext(project_id="p", session_id="s", tenant_id="t",
                         args=args, project=project)
    return list(cmd.handler(ctx))


def _card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def _step(id="s1", prompt="do thing", condition=None, parallel_with=None):
    return SimpleNamespace(id=id, prompt=prompt, condition=condition,
                           parallel_with=parallel_with, on_failure=None,
                           max_retries=0, metadata={})


def _wf(name="release", status="running", steps=None, results=None,
        description="release flow", current_step="s1"):
    return SimpleNamespace(
        id="wf1", name=name, steps=steps or [_step()],
        description=description, state={"branch": "main"},
        results=results or {}, current_step=current_step, status=status,
        to_dict=lambda: {"id": "wf1", "name": name},
    )


def test_workflow_no_active_yields_empty_state_card():
    events = _run(project=SimpleNamespace(active_workflow=None))
    card = _card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


def test_workflow_active_yields_list_card():
    project = SimpleNamespace(active_workflow=_wf())
    events = _run(project=project)
    card = _card(events)
    assert card is not None
    assert card["id"] == "workflow"
    assert card["variant"] == "list"
    assert card["icon"] == "workflow"
    items = card["payload"]["items"]
    assert items[0]["title"] == "s1"


def test_workflow_card_step_done_has_ok_badge():
    project = SimpleNamespace(active_workflow=_wf(
        steps=[_step("s1"), _step("s2")],
        results={"s1": "ok"},
    ))
    events = _run(project=project)
    card = _card(events)
    items = {it["title"]: it for it in card["payload"]["items"]}
    s1_badges = [b["text"].lower() for b in items["s1"]["badges"]]
    s2_badges = [b["text"].lower() for b in items["s2"]["badges"]]
    assert any("done" in t or "complete" in t for t in s1_badges)
    assert any("pending" in t or "todo" in t for t in s2_badges)


def test_workflow_card_step_subtitle_is_first_prompt_line():
    project = SimpleNamespace(active_workflow=_wf(
        steps=[_step("s1", prompt="First line of prompt\nsecond line")],
    ))
    events = _run(project=project)
    card = _card(events)
    item = card["payload"]["items"][0]
    assert "First line of prompt" in (item.get("subtitle") or "")


def test_workflow_card_summary_has_status():
    project = SimpleNamespace(active_workflow=_wf(status="running"))
    events = _run(project=project)
    card = _card(events)
    summary = card["payload"]["summary"] or ""
    assert "running" in summary.lower()


def test_workflow_card_includes_step_count_in_summary():
    project = SimpleNamespace(active_workflow=_wf(
        steps=[_step("s1"), _step("s2"), _step("s3")],
        results={"s1": "ok"},
    ))
    events = _run(project=project)
    card = _card(events)
    summary = card["payload"]["summary"] or ""
    # Should report both total and completed.
    assert "3" in summary
    assert "1" in summary


def test_workflow_card_conditional_step_meta():
    project = SimpleNamespace(active_workflow=_wf(
        steps=[_step("s1", condition="branch == main")],
    ))
    events = _run(project=project)
    card = _card(events)
    item = card["payload"]["items"][0]
    blob = (item.get("meta") or "") + (item.get("subtitle") or "")
    assert "branch == main" in blob or "condition" in blob.lower()
