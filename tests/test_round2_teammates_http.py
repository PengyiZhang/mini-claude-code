"""Round 2 — teammate lifecycle via /agents slash commands over HTTP.

Exercises the deterministic shell entry added to fix the YOLO audit's
"Bug 2 — no /agents spawn subcommand":

    POST /tenants/{tid}/projects/{pid}/sessions/{sid}/commands/agents
         body: {"args": "spawn <name> <role> --prompt <text>"}
    POST .../commands/agents      body: {"args": ""}             # list
    POST .../commands/agents      body: {"args": "stop <name>"}
    POST .../commands/agents      body: {"args": "inbox <name>"}

Avoids burning real LLM tokens by monkeypatching the teammate
loop_factory to a no-op fake. The teammate's daemon thread exits
immediately; we verify the *spawner* state machine and slash-command
protocol.
"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import manager as pm_mod
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


class _FakeLoop:
    """Stand-in for AgentLoop: exit immediately, no LLM calls."""

    def run(self, prompt, *, on_event=None) -> str:
        return "noop"

    def set_worktree(self, *a, **kw): ...
    def as_ref(self) -> object: return None


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Replace the real teammate loop factory with a no-op so the test
    # never calls out to LiteLLM/Anthropic.
    monkeypatch.setattr(pm_mod, "_build_teammate_loop",
                        lambda project, sid: _FakeLoop())

    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    return TestClient(build_app(
        data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
    ))


def _cmd(client, sid="s1", pid="p1", args="") -> list[dict]:
    """Run /agents with args, return parsed SSE events."""
    r = client.post(
        f"/tenants/tenant1/projects/{pid}/sessions/{sid}/commands/agents",
        headers=AUTH, json={"args": args})
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


def _setup(client, pid="p1"):
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": pid})
    client.post(f"/tenants/tenant1/projects/{pid}/sessions",
                headers=AUTH, json={"session_id": "s1"})


def test_agents_spawn_then_list_then_stop(app):
    _setup(app)
    # spawn
    ev = _cmd(app, args='spawn alice researcher --prompt "noop and exit"')
    text = "".join(e.get("text", "") for e in ev if e.get("type") == "text")
    assert "Teammate 'alice' spawned" in text
    assert ev[-1]["type"] == "done", f"expected done footer; got {ev[-1]}"

    # Wait for the spawn thread to mark alice alive in the registry.
    deadline = time.time() + 2
    listed_text = ""
    while time.time() < deadline:
        ev = _cmd(app, args="")
        listed_text = "".join(e.get("text", "") for e in ev if e.get("type") == "text")
        if "alice" in listed_text:
            break
        time.sleep(0.05)
    assert "alice" in listed_text
    assert "researcher" in listed_text

    # stop — should succeed whether alice is still alive or already
    # exited (the registry retains stopped entries briefly).
    ev = _cmd(app, args="stop alice")
    # Either shutdown request sent, or "not found" if alice already
    # exited — both are acceptable, but the protocol must end in done.
    assert ev[-1]["type"] == "done"


def test_agents_spawn_unknown_subcommand_emits_done(app):
    """Regression for the bug where /agents error paths returned without
    emitting `done`, leaving the front-end command runner hanging."""
    _setup(app)
    ev = _cmd(app, args="frobnicate x")
    types = [e["type"] for e in ev]
    assert "error" in types
    assert types[-1] == "done"


def test_agents_spawn_missing_prompt_usage_error(app):
    _setup(app)
    ev = _cmd(app, args="spawn bob researcher")
    err = next((e for e in ev if e.get("type") == "error"), None)
    assert err is not None
    assert "usage" in err["message"].lower()
    assert ev[-1]["type"] == "done"


def test_agents_inbox_renders_real_keys(app):
    """Regression for the mock-drift bug where the inbox reader used
    `from_agent`/`kind` but MessageBus writes `from`/`type`. Spawns a
    teammate, sends it a real shutdown_request, then peeks its inbox —
    sender must render as 'lead', not '?'."""
    _setup(app)
    _cmd(app, args='spawn bob worker --prompt "noop"')
    # Wait for bob to wire up.
    deadline = time.time() + 2
    while time.time() < deadline:
        ev = _cmd(app, args="")
        text = "".join(e.get("text", "") for e in ev if e.get("type") == "text")
        if "bob" in text:
            break
        time.sleep(0.05)
    # Send shutdown_request — that goes into bob's inbox with sender
    # 'lead' and type 'shutdown_request' (real MessageBus keys).
    _cmd(app, args="stop bob")

    ev = _cmd(app, args="inbox bob")
    text = "".join(e.get("text", "") for e in ev if e.get("type") == "text")
    # The render must never show the placeholder `?` for the sender.
    assert "?" not in text, (
        f"inbox still rendering placeholder sender; got: {text!r}")
