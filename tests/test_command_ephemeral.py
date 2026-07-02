"""Ephemeral flag for /commands — panel polling must not pollute the
session transcript with one card bubble per poll tick.

Regression (Bug 2): TeammatesPanel polls /agents every 4s via
runCommandForCard. The server persisted every emitted card as a
synthetic __card__ tool_use, so polling for a minute wrote 30 bubbles
to the transcript. On page refresh, hydrate replayed all of them and
the chat pane filled with duplicate agents-roster cards.

Fix: add ``ephemeral`` to RunCommandRequest. When true, the route
skips persist_card_event and save_messages — the SSE stream still
delivers the card live, but nothing is written to disk.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def app(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    return TestClient(build_app(
        data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
    ))


def _setup(client, pid="p1"):
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": pid})
    client.post(f"/tenants/tenant1/projects/{pid}/sessions",
                headers=AUTH, json={"session_id": "s1"})


def _cmd(client, args="", ephemeral=False, sid="s1", pid="p1"):
    body = {"args": args}
    if ephemeral:
        body["ephemeral"] = True
    r = client.post(
        f"/tenants/tenant1/projects/{pid}/sessions/{sid}/commands/agents",
        headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def _session_messages(client, sid="s1", pid="p1"):
    """Return raw persisted messages for the session."""
    r = client.get(
        f"/tenants/tenant1/projects/{pid}/sessions/{sid}/messages",
        headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # Endpoint may return either a bare list or {"messages": [...]}.
    return body["messages"] if isinstance(body, dict) else body


def _count_card_blocks(messages):
    """Count persisted __card__ tool_use blocks."""
    n = 0
    for m in messages:
        if not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("name") == "__card__":
                n += 1
    return n


def test_ephemeral_command_does_not_persist_card(app):
    """A panel-poll /agents call with ephemeral=true streams the card
    live (so the panel sees fresh data) but writes nothing to the
    transcript — refresh won't replay a flood of stale roster snapshots."""
    _setup(app)
    events = _cmd(app, args="", ephemeral=True)
    # Live stream still carries the card so the panel can render.
    assert any(e.get("type") == "card" for e in events)
    # But the transcript stays clean.
    msgs = _session_messages(app)
    assert _count_card_blocks(msgs) == 0


def test_non_ephemeral_command_persists_card(app):
    """Default behavior (no ephemeral flag) preserves the existing
    'cards survive refresh' contract — ephemeral must be opt-in only."""
    _setup(app)
    events = _cmd(app, args="")  # ephemeral defaults to False
    assert any(e.get("type") == "card" for e in events)
    msgs = _session_messages(app)
    assert _count_card_blocks(msgs) >= 1


def test_ephemeral_then_persisted_does_not_double_count(app):
    """Mixed polling + intentional /agents calls: only the persisted
    one writes to the transcript. Simulates the panel polling in the
    background while the user explicitly runs /agents from the chat
    box once."""
    _setup(app)
    _cmd(app, args="", ephemeral=True)
    _cmd(app, args="", ephemeral=True)
    _cmd(app, args="")  # user-initiated
    msgs = _session_messages(app)
    # Exactly one persisted card (from the non-ephemeral call).
    assert _count_card_blocks(msgs) == 1
