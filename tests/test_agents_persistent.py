"""Task 1 (debug.7.md): 手工 spawn 的 teammate 应当常驻。

默认 ``persistent=True``：idle_timeout 触发后不退出，继续等待新工作；
显式 ``persistent=False`` 才沿用旧的"超时即退出"行为。

同时覆盖 Task 1d：``/agents delete`` 与 ``/agents edit`` 子命令。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from mini_cc.commands import CommandContext, default_registry
from mini_cc.teams import TeammateSpawner


# ── Spawner-level: persistent flag ───────────────────────────────────────


class _FakeLoop:
    """Replays a script of events each turn, then yields done."""

    def __init__(self, script):
        self.script = list(script)
        self.runs: list[str] = []

    def run(self, user_input):
        self.runs.append(user_input)
        for ev in self.script:
            yield ev


def test_spawn_persistent_default_keeps_alive_after_idle_timeout(tmp_path):
    """persistent=True (the new default): idle_timeout fires but the
    teammate stays alive, ready to receive new work."""
    loop = _FakeLoop([{"type": "done"}])
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    err = spawner.spawn("alice", "researcher", "hi")
    assert err is None
    # Wait long enough for at least 2 idle_timeout cycles (0.1s each).
    deadline = time.time() + 0.6
    while time.time() < deadline:
        if not spawner.list_alive():
            break
        time.sleep(0.05)
    # Must STILL be alive — persistent default.
    assert spawner.list_alive(), (
        "persistent teammate exited after idle_timeout — should stay alive")
    # Cleanup
    spawner.request_shutdown("alice")
    deadline = time.time() + 2
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)


def test_spawn_explicit_persistent_false_exits_on_timeout(tmp_path):
    """persistent=False: legacy behavior — teammate exits after idle_timeout."""
    loop = _FakeLoop([{"type": "done"}])
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    err = spawner.spawn("bob", "worker", "hi", persistent=False)
    assert err is None
    deadline = time.time() + 2
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert spawner.list_alive() == [], (
        "non-persistent teammate should exit on idle_timeout")


def test_persistent_teammate_wakes_on_new_message(tmp_path):
    """A persistent teammate that timed out once should still pick up a
    new inbox message on the next idle poll cycle and run another turn."""
    # Script yields two done events across two turns (one per run() call).
    script = [{"type": "done"}]
    loop = _FakeLoop(script)
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: loop,
        idle_poll_interval=0.02, idle_timeout=0.1)
    spawner.spawn("carol", "worker", "first")
    # Wait for first turn + at least one idle poll cycle.
    time.sleep(0.2)
    # Drop a message into carol's inbox — should wake her.
    spawner.bus.send("lead", "carol", "second task", "message")
    deadline = time.time() + 1.5
    while time.time() < deadline and len(loop.runs) < 2:
        time.sleep(0.02)
    assert len(loop.runs) >= 2, (
        "persistent teammate did not wake on new inbox message")
    spawner.request_shutdown("carol")
    deadline = time.time() + 2
    while spawner.list_alive() and time.time() < deadline:
        time.sleep(0.02)


# ── /agents delete + edit subcommands (Task 1d) ─────────────────────────


def _run_cmd(name, *, project=None, args=""):
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None
    ctx = CommandContext(
        project_id="p", session_id="s", tenant_id="t",
        args=args, project=project,
    )
    return list(cmd.handler(ctx))


def test_agents_delete_removes_stopped_teammate():
    """`/agents delete <name>` drops a stopped teammate from the registry.

    Alive teammates must be stopped first — delete refuses to remove a
    still-running teammate (force-delete would race the worker thread).
    """
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "r"
        alive: bool = False
        started_at: float = 0.0
        stopped_at: float = 1.0
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info()}
        def list_alive(self): return []
        def delete(self, name):
            existed = name in self._teammates
            self._teammates.pop(name, None)
            return None if existed else f"Teammate '{name}' not found"
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="delete alice")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "deleted" in text.lower() or "removed" in text.lower()
    assert "alice" not in _P.teams._teammates


def test_agents_delete_refuses_alive_teammate():
    """`/agents delete` on a still-alive teammate must error and tell the
    user to stop it first."""
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "r"
        alive: bool = True
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info()}
        def list_alive(self): return list(self._teammates.values())
        def delete(self, name):
            raise AssertionError("delete must not be called on alive teammate")
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="delete alice")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "stop" in err["message"].lower() or "alive" in err["message"].lower()


def test_agents_delete_unknown_teammate_errors():
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
        def delete(self, name): return f"Teammate '{name}' not found"
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="delete ghost")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "not found" in err["message"].lower()


def test_agents_edit_updates_role_and_prompt():
    """`/agents edit <name> --role <r> --prompt <p>` updates a stopped
    teammate's stored definition so the next spawn uses the new values.

    Edit on alive teammate is refused (mid-flight edits would be racy).
    """
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "old_role"
        alive: bool = False
        prompt: str = "old"
    captured = {}
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info()}
        def list_alive(self): return []
        def edit(self, name, *, role=None, prompt=None):
            t = self._teammates.get(name)
            if t is None:
                return f"Teammate '{name}' not found"
            if role is not None:
                t.role = role
            if prompt is not None:
                t.prompt = prompt
            captured.update(role=role, prompt=prompt)
            return None
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd(
        "agents", project=_P(),
        args='edit alice --role new_role --prompt "better prompt"')
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "edit" in text.lower() or "updated" in text.lower()
    assert captured["role"] == "new_role"
    assert captured["prompt"] == "better prompt"


def test_agents_edit_unknown_teammate_errors():
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
        def edit(self, name, **kw): return f"Teammate '{name}' not found"
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="edit ghost --role x")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "not found" in err["message"].lower()


def test_agents_roster_includes_delete_and_edit_menu_actions():
    """Stopped teammates should expose edit + delete in their row menu;
    alive teammates expose stop. Together the roster covers all three
    lifecycle actions (debug.7.md Task 1)."""
    @dataclass
    class _Info:
        name: str
        role: str = "r"
        alive: bool = True
        started_at: float = 0.0
    class _Spawner:
        def __init__(self):
            self._teammates = {
                "alice": _Info("alice"),                 # alive
                "bob": _Info("bob", alive=False),        # stopped
            }
        def list_alive(self): return [t for t in self._teammates.values()
                                      if t.alive]
        bus = None
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P())
    card = next(e for e in events if e.get("type") == "card")
    items = {it["title"]: it for it in card["payload"]["items"]}
    # Alice (alive): stop
    alice_menu = {a["label"] for a in items["alice"]["menu"]}
    assert "stop" in alice_menu
    # Bob (stopped): edit + delete
    bob_menu = {a["label"] for a in items["bob"]["menu"]}
    assert "edit" in bob_menu
    assert "delete" in bob_menu
