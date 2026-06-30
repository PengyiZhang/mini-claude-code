"""Phase H — /agents enhancements + workflow_run_all + /workflow + /bg."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from mini_cc.commands import CommandContext, default_registry
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext
from mini_cc.tools.background import BackgroundScheduler
from mini_cc.workflow import workflow_from_dict


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def sandbox(tmp_path):
    return SubprocessSandbox("proj-h", tmp_path / "ws")


@pytest.fixture
def storage(tmp_path):
    return FSStorage(tmp_path / "state")


@pytest.fixture
def ctx(sandbox, storage):
    return ToolContext(
        project_id="proj-h", session_id="sess-h",
        sandbox=sandbox, storage=storage, todos=[],
    )


def _run_cmd(name, *, project=None, args=""):
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None, f"/{name} not registered"
    ctx = CommandContext(
        project_id="proj-h", session_id="sess-h",
        tenant_id="t-h", args=args, project=project,
    )
    return list(cmd.handler(ctx))


# ── /agents enhancements ─────────────────────────────────────────────────

def test_agents_slash_registered():
    reg = default_registry()
    assert reg.resolve("agents") is not None


def test_agents_command_lists_status_and_inbox_count():
    """Teammate with empty inbox shows 📨0 (or omits the tag).

    We check the alive-marker and the role render; inbox tag may be
    absent when the count is 0.
    """
    @dataclass
    class _Info:
        name: str
        role: str
        alive: bool = True
        started_at: float = field(default_factory=time.time)

    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info("alice", "researcher")}
        def list_alive(self):
            return [t for t in self._teammates.values() if t.alive]
        @property
        def bus(self):
            return None  # _count_inbox returns 0

    class _P:
        teams = _Spawner()
        tenant_id = "t1"

    events = _run_cmd("agents", project=_P())
    # Since the rich-card migration /agents emits a list card event
    # instead of a text blob. Assert against the card payload.
    card = next(e for e in events if e.get("type") == "card")
    items = card["payload"]["items"]
    alice = next(it for it in items if it["title"] == "alice")
    assert alice["subtitle"] and "researcher" in alice["subtitle"]
    badge_texts = [b["text"] for b in alice["badges"]]
    assert "alive" in badge_texts  # was 🟢 in the old text format
    summary = card["payload"].get("summary") or ""
    assert "1 " in summary and "alive" in summary


def test_agents_command_stop_calls_request_shutdown():
    """`/agents stop <name>` delegates to spawner.request_shutdown."""
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "r"
        alive: bool = True
    class _Spawner:
        def __init__(self):
            self.shutdown_called_with = None
            self._teammates = {"alice": _Info()}
        def list_alive(self):
            return list(self._teammates.values())
        def request_shutdown(self, name):
            self.shutdown_called_with = name
            return f"Shutdown request sent to {name}"
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="stop alice")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "Shutdown request sent to alice" in text
    assert _P.teams.shutdown_called_with == "alice"


def test_agents_command_stop_missing_name_errors():
    class _Spawner: pass
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="stop")
    # No name → usage error message.
    err = next((e for e in events if e.get("type") == "error"), None)
    # OR falls through to the unknown-subcommand branch since args[0]=="stop"
    # but len(parts)<2 → not matched, falls to default unknown.
    assert err is not None or any("unknown" in (e.get("text", "")).lower()
                                   for e in events)


def test_agents_command_inbox_peek():
    """`/agents inbox <name>` shows inbox contents via bus.peek_inbox.

    Uses the REAL MessageBus serialization keys (`from` / `type`) —
    earlier versions of this test used `from_agent` / `kind`, which
    matched a buggy reader in `_cmd_agents` and masked a P1 production
    bug where the rendered inbox always showed `? (message): ...`.
    """
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "r"
        alive: bool = True
    class _Bus:
        def peek_inbox(self, name):
            return [
                {"from": "lead", "type": "shutdown_request",
                 "content": "Please stop now."},
                {"from": "bob", "type": "message",
                 "content": "FYI"},
            ]
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info()}
            self.bus = _Bus()
        def list_alive(self):
            return list(self._teammates.values())
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="inbox alice")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "Inbox for `alice`" in text
    assert "lead" in text
    assert "Please stop now" in text
    # Sender and type render correctly (regression for the
    # `from_agent`/`kind` mock-drift bug).
    assert "(shutdown_request)" in text
    assert "(message)" in text
    assert "?" not in text  # no placeholder sender


def test_agents_command_error_paths_emit_done():
    """Every /agents error branch must emit `done` after `error`,
    otherwise the front-end command runner hangs on onDone (commands.ts).
    Covers: unknown subcommand, shutdown failure, unknown teammate."""
    @dataclass
    class _Info:
        name: str = "alice"
        role: str = "r"
        alive: bool = True
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info()}
        def list_alive(self):
            return list(self._teammates.values())
        def request_shutdown(self, name):
            raise RuntimeError("bus is down")
        bus = None
    class _P:
        teams = _Spawner()
        tenant_id = "t1"

    # Unknown subcommand.
    evts = _run_cmd("agents", project=_P(), args="frobnicate x")
    types = [e["type"] for e in evts]
    assert "error" in types
    assert types[-1] == "done", f"error path must end with done; got {types}"

    # Shutdown failure.
    evts = _run_cmd("agents", project=_P(), args="stop alice")
    types = [e["type"] for e in evts]
    assert "error" in types
    assert types[-1] == "done", f"shutdown-error must end with done; got {types}"

    # Unknown teammate (no bus).
    evts = _run_cmd("agents", project=_P(), args="inbox nobody")
    types = [e["type"] for e in evts]
    assert "error" in types
    assert types[-1] == "done", f"unknown-teammate must end with done; got {types}"


def test_agents_command_spawn_delegates_to_spawner():
    """`/agents spawn <name> <role> --prompt <text>` calls spawner.spawn
    deterministically — previously the only spawn path was the
    `spawn_teammate` tool, which required burning LLM tokens to invoke.
    Testers (and the e2e suite) need a deterministic shell entry.
    """
    captured = {}

    @dataclass
    class _Info:
        name: str = ""
        role: str = ""
        alive: bool = True
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
        def spawn(self, name, role, prompt, on_event=None):
            captured.update(name=name, role=role, prompt=prompt,
                            on_event=on_event)
            return None  # success
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd(
        "agents", project=_P(),
        args='spawn bob researcher --prompt "find the bug"',
    )
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "Teammate 'bob' spawned" in text
    assert captured["name"] == "bob"
    assert captured["role"] == "researcher"
    assert captured["prompt"] == "find the bug"


def test_agents_command_spawn_reports_conflict():
    """Spawn with an in-use name returns the spawner's error string."""
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
        def spawn(self, name, role, prompt, on_event=None):
            return f"Teammate '{name}' already exists"
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd(
        "agents", project=_P(),
        args="spawn dup r --prompt x",
    )
    err = next((e for e in events if e.get("type") == "error"), None)
    # Either an error event or text containing the conflict message.
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    assert err is not None or "already exists" in text


def test_agents_command_spawn_missing_args_errors():
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
        def spawn(self, *a, **kw): raise AssertionError("must not spawn")
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="spawn only_name")
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "done"


def test_agents_command_inbox_unknown_teammate():
    class _Bus:
        def peek_inbox(self, name):
            return []   # empty, but valid → no teammate means no msgs
    class _Spawner:
        bus = _Bus()
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="inbox nobody")
    # Empty inbox → friendly "empty inbox" text.
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "empty" in text.lower()


def test_agents_unknown_subcommand_errors():
    """`/agents frobnicate` → unknown-subcommand error."""
    class _Spawner:
        _teammates = {}
        def list_alive(self): return []
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P(), args="frobnicate x")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "unknown" in err["message"].lower()


# ── /workflow slash command ──────────────────────────────────────────────

def test_workflow_slash_registered():
    reg = default_registry()
    assert reg.resolve("workflow") is not None


def test_workflow_command_no_active():
    class _P: pass
    events = _run_cmd("workflow", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "no active" in text.lower()


def test_workflow_command_shows_active():
    wf = workflow_from_dict({
        "name": "demo",
        "description": "A demo workflow",
        "steps": [
            {"id": "s1", "prompt": "do the first thing"},
            {"id": "s2", "prompt": "do the second thing",
             "condition": "s1"},
        ],
    })
    wf.results["s1"] = "completed output"

    class _P:
        active_workflow = wf
    events = _run_cmd("workflow", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "demo" in text
    assert "A demo workflow" in text
    assert "s1" in text and "s2" in text
    assert "✅" in text  # s1 completed marker
    assert "if `s1`" in text  # condition annotation


def test_workflow_command_clear():
    wf = workflow_from_dict({"name": "x", "steps": []})
    class _P:
        active_workflow = wf
    events = _run_cmd("workflow", project=_P(), args="clear")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "cleared" in text.lower()


# ── /bg slash command ────────────────────────────────────────────────────

def test_bg_slash_registered():
    reg = default_registry()
    assert reg.resolve("bg") is not None


def test_bg_command_no_scheduler():
    class _P: pass
    events = _run_cmd("bg", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "not configured" in text.lower()


def test_bg_command_empty():
    class _P:
        background = BackgroundScheduler()
    events = _run_cmd("bg", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "no background tasks" in text.lower()


def test_bg_command_lists_tasks():
    """Migrated in Plan B.5 — /bg now emits a list card."""
    bg = BackgroundScheduler()
    from mini_cc.tools.background import _BGTask
    bg._tasks["bg_a"] = _BGTask("bg_a", "tu1", "bash",
                                "echo first", status="running")
    bg._tasks["bg_b"] = _BGTask("bg_b", "tu2", "bash",
                                "echo second", status="completed")
    class _P:
        background = bg
    events = _run_cmd("bg", project=_P())
    card = next(e for e in events if e.get("type") == "card")
    blob = repr(card["payload"])
    assert "bg_a" in blob
    assert "bg_b" in blob
    assert "echo first" in blob


def test_bg_command_stop_dispatches():
    bg = BackgroundScheduler()
    from mini_cc.tools.background import _BGTask
    bg._tasks["bg_x"] = _BGTask("bg_x", "tu1", "bash",
                                "echo x", status="running")
    class _P:
        background = bg
    events = _run_cmd("bg", project=_P(), args="stop bg_x")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "stop requested" in text.lower()
    assert bg._tasks["bg_x"].status == "stopped"


def test_bg_command_stop_unknown():
    bg = BackgroundScheduler()
    class _P:
        background = bg
    events = _run_cmd("bg", project=_P(), args="stop bg_nope")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "unknown" in text.lower() or "Error" in text


# ── workflow_run_all tool ────────────────────────────────────────────────

def test_workflow_run_all_registered():
    tools = dispatch(builtin_tools())
    assert "workflow_run_all" in tools


def test_workflow_run_all_no_active_workflow(ctx):
    class _P: pass
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_all"].handle(ctx, {})
    assert "Error" in out
    assert "no active" in out.lower()


def test_workflow_run_all_no_dispatcher(ctx):
    """Without a dispatcher wired, returns a clear error."""
    class _P:
        active_workflow = workflow_from_dict({
            "name": "x",
            "steps": [{"id": "a", "prompt": "A"}],
        })
    ctx.project_ref = _P
    # No workflow_dispatch on ctx.
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_all"].handle(ctx, {})
    assert "Error" in out
    assert "dispatcher" in out.lower()


def test_workflow_run_all_walks_all_steps(ctx):
    """With dispatcher wired, runs every step in order and records results."""
    seen: list[str] = []

    def dispatcher(prompt: str) -> str:
        seen.append(prompt)
        return f"ran:{prompt}"

    class _P:
        active_workflow = workflow_from_dict({
            "name": "demo",
            "steps": [
                {"id": "a", "prompt": "do A"},
                {"id": "b", "prompt": "then B with {a}"},
                {"id": "c", "prompt": "finally C with {b}"},
            ],
        })
    ctx.project_ref = _P
    ctx.workflow_dispatch = dispatcher  # type: ignore
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_all"].handle(ctx, {})

    assert "run_all — done" in out
    # All three dispatched.
    assert len(seen) == 3
    # Second prompt got the first result substituted.
    assert "ran:do A" in seen[1]
    # Workflow state reflects completion.
    assert _P.active_workflow.results["c"].startswith("ran:finally")
    # Summary includes step markers.
    assert "✅ a" in out
    assert "✅ c" in out


def test_workflow_run_all_handles_abort(ctx):
    """If a step raises and on_failure=abort, the run stops there."""
    def dispatcher(prompt: str) -> str:
        if "B" in prompt:
            raise RuntimeError("boom")
        return "ok"

    class _P:
        active_workflow = workflow_from_dict({
            "name": "x",
            "steps": [
                {"id": "a", "prompt": "do A"},
                {"id": "b", "prompt": "do B", "on_failure": "abort"},
                {"id": "c", "prompt": "do C"},
            ],
        })
    ctx.project_ref = _P
    ctx.workflow_dispatch = dispatcher  # type: ignore
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_all"].handle(ctx, {})
    assert "aborted" in out.lower()
    assert "❌ b" in out
    # C never ran.
    assert "c" not in _P.active_workflow.results


def test_workflow_run_all_skips_false_conditions(ctx):
    def dispatcher(prompt: str) -> str:
        return "ok"
    class _P:
        active_workflow = workflow_from_dict({
            "name": "x",
            "steps": [
                {"id": "a", "prompt": "do A"},
                {"id": "b", "prompt": "do B", "condition": "never_set"},
                {"id": "c", "prompt": "do C"},
            ],
        })
    ctx.project_ref = _P
    ctx.workflow_dispatch = dispatcher  # type: ignore
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_all"].handle(ctx, {})
    # b was skipped, a and c completed.
    assert "⏭ b" in out
    assert "✅ a" in out
    assert "✅ c" in out


# ── Loop runner dispatch wired in AgentLoop._make_ctx ────────────────────

def test_make_ctx_attaches_workflow_dispatch(tmp_path):
    """AgentLoop._make_ctx should attach a callable workflow_dispatch
    that drives a sub-agent via spawn_subagent."""
    from mini_cc.core.loop import AgentLoop, ProjectRef
    from mini_cc.sandbox import SubprocessSandbox
    from mini_cc.storage import FSStorage

    sandbox = SubprocessSandbox("proj-x", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(
        project_id="proj-x", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: None,
    )
    loop = AgentLoop(ref, "sess-x")
    ctx = loop._make_ctx()
    assert callable(getattr(ctx, "workflow_dispatch", None))


# ── /help enumerates the new commands ────────────────────────────────────

def test_help_lists_new_commands():
    """Every new slash command must appear in /help output."""
    reg = default_registry()
    visible = {c.name for c in reg.all_visible()}
    for name in ("agents", "workflow", "bg"):
        assert name in visible, f"/{name} not visible in help"
