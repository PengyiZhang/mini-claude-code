"""Tests for /agents roster emitting a list card."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


def _run_agents_with_spawner(spawner, project_id: str = "p1"):
    from mini_cc.commands import default_registry
    from mini_cc.commands.registry import CommandContext

    class _P:
        teams = spawner
        tenant_id = "t1"
    reg = default_registry()
    cmd = reg.resolve("agents")
    ctx = CommandContext(
        project_id=project_id,
        session_id="s",
        tenant_id="t",
        args="",
        project=_P(),
    )
    return list(cmd.handler(ctx))


def _extract_card(events: list[dict]) -> dict | None:
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


@dataclass
class _Info:
    name: str = "alice"
    role: str = "researcher"
    alive: bool = True
    started_at: float = field(default_factory=time.time)
    worktree: str | None = None


class _Bus:
    def __init__(self, counts: dict[str, int] | None = None):
        self._counts = counts or {}

    def peek_inbox(self, who):
        return [{"from": "lead"} for _ in range(self._counts.get(who, 0))]


class _Spawner:
    def __init__(self, infos, counts=None):
        self._teammates = {i.name: i for i in infos}
        self.bus = _Bus(counts or {})

    def list_alive(self):
        return [i for i in self._teammates.values() if i.alive]


def test_agents_roster_yields_card_when_teammates_exist():
    spawner = _Spawner([_Info(name="alice", role="researcher"),
                       _Info(name="bob", role="coder", alive=False)])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None, f"no card in /agents output: {events!r}"
    assert card["id"] == "agents-roster"
    assert card["variant"] == "list"
    assert card["icon"] == "agents"
    assert card["status"] == "ok"


def test_agents_roster_card_has_item_per_teammate():
    spawner = _Spawner([_Info(name="alice"), _Info(name="bob")])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    items = card["payload"]["items"]
    assert {it["title"] for it in items} == {"alice", "bob"}


def test_agents_roster_carries_inbox_count_badge():
    spawner = _Spawner(
        [_Info(name="alice")],
        counts={"alice": 3},
    )
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    alice = card["payload"]["items"][0]
    badge_texts = [b["text"] for b in alice["badges"]]
    assert any("3" in t for t in badge_texts), (
        f"inbox count badge missing; got {badge_texts!r}"
    )


def test_agents_roster_expandable_command_links_to_inbox():
    spawner = _Spawner([_Info(name="alice")])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    alice = card["payload"]["items"][0]
    assert alice["expandable_command"] == "/agents inbox alice"


def test_agents_roster_has_spawn_action():
    spawner = _Spawner([_Info(name="alice")])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    cmds = [a["command"] for a in card["actions"]]
    assert any(c.startswith("/agents spawn") for c in cmds), cmds


def test_agents_roster_summary_counts_alive_and_stopped():
    spawner = _Spawner([
        _Info(name="alice", alive=True),
        _Info(name="bob", alive=True),
        _Info(name="carol", alive=False),
    ])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    summary = card["payload"].get("summary") or ""
    assert "2" in summary and "1" in summary, summary


def test_agents_roster_empty_state_when_no_teammates():
    spawner = _Spawner([])
    events = _run_agents_with_spawner(spawner)
    card = _extract_card(events)
    assert card is not None
    assert card["payload"]["items"] == []
    # empty_hint is the placeholder shown in the UI when no teammates.
    assert card["payload"].get("empty_hint"), "empty_hint should be set"


def test_agents_roster_unconfigured_emits_text_not_card():
    """When the project has no teams subsystem, /agents should still
    emit a friendly text marker instead of crashing or emitting an
    empty card."""
    class _P:
        teams = None
    from mini_cc.commands import default_registry
    from mini_cc.commands.registry import CommandContext
    reg = default_registry()
    cmd = reg.resolve("agents")
    ctx = CommandContext(
        project_id="p", session_id="s", tenant_id="t", args="", project=_P()
    )
    events = list(cmd.handler(ctx))
    assert any(e.get("type") == "text" for e in events)
    assert not any(e.get("type") == "card" for e in events)
