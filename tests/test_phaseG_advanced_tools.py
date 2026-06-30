"""Phase G tests — WebSearch / WakeupScheduler / LSP / Workflow."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from mini_cc.commands import CommandContext, default_registry
from mini_cc.config import AnthropicConfig, set_default_config
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.scheduler import WakeupScheduler
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext
from mini_cc.workflow import (Workflow, WorkflowStep, run_workflow,
                              workflow_from_dict, workflow_from_markdown)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def sandbox(tmp_path):
    return SubprocessSandbox("proj-g", tmp_path / "ws")


@pytest.fixture
def storage(tmp_path):
    return FSStorage(tmp_path / "state")


@pytest.fixture
def ctx(sandbox, storage):
    return ToolContext(
        project_id="proj-g", session_id="sess-g",
        sandbox=sandbox, storage=storage, todos=[],
    )


# ── WebSearch ────────────────────────────────────────────────────────────

def test_web_search_requires_api_key(ctx):
    """No key configured → clear 'not configured' error."""
    set_default_config(AnthropicConfig())  # no tavily_api_key
    tools = dispatch(builtin_tools())
    out = tools["web_search"].handle(ctx, {"query": "python"})
    assert "Error" in out
    assert "TAVILY_API_KEY" in out


def test_web_search_rejects_empty_query(ctx):
    set_default_config(AnthropicConfig(tavily_api_key="fake"))
    tools = dispatch(builtin_tools())
    out = tools["web_search"].handle(ctx, {"query": ""})
    assert "Error" in out
    assert "query" in out.lower()


def test_web_search_calls_tavily_endpoint(ctx, monkeypatch):
    """With a key set, the tool POSTs to Tavily and returns formatted text."""
    set_default_config(AnthropicConfig(tavily_api_key="key_xxx"))
    captured: dict = {}

    class _Resp:
        status = 200
        headers = {"Content-Type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n):
            payload = {
                "answer": "Python is a language.",
                "results": [
                    {"title": "Python docs", "url": "https://python.org",
                     "content": "Welcome to Python.", "score": 0.95},
                ],
            }
            data = json.dumps(payload).encode("utf-8")
            return data[:n]

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Resp()

    import mini_cc.tools.websearch as _ws
    monkeypatch.setattr(_ws.urllib.request, "urlopen", fake_urlopen)

    tools = dispatch(builtin_tools())
    out = tools["web_search"].handle(ctx, {"query": "python",
                                           "max_results": 3,
                                           "include_answer": True})
    assert "Tavily" not in out or "Error" not in out
    assert "Python docs" in out
    assert "python.org" in out
    assert "Welcome to Python" in out
    assert "Quick answer" in out
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["data"]["api_key"] == "key_xxx"
    assert captured["data"]["query"] == "python"
    assert captured["data"]["max_results"] == 3


def test_web_search_handles_http_error(ctx, monkeypatch):
    """An HTTP error from Tavily surfaces as an Error: line."""
    set_default_config(AnthropicConfig(tavily_api_key="bad"))
    import urllib.error as _ue

    def fake_urlopen(req, timeout=None):
        raise _ue.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    import mini_cc.tools.websearch as _ws
    monkeypatch.setattr(_ws.urllib.request, "urlopen", fake_urlopen)

    tools = dispatch(builtin_tools())
    out = tools["web_search"].handle(ctx, {"query": "x"})
    assert "Error" in out
    assert "401" in out


# ── WakeupScheduler ──────────────────────────────────────────────────────

def test_wakeup_scheduler_schedules_and_fires():
    """Use base_time + explicit tick(now) to dodge wall-clock flakiness."""
    sched = WakeupScheduler()
    base = 1000.0
    wid = sched.schedule("check back", 5, reason="test", base_time=base)
    assert wid.startswith("wakeup_")
    # At t=base+1, not due yet (fire_at = base+5).
    assert sched.tick(now=base + 1) == []
    # At t=base+5, fires.
    due = sched.tick(now=base + 5)
    assert len(due) == 1
    assert due[0].wakeup_id == wid
    assert due[0].prompt == "check back"
    assert due[0].reason == "test"
    # Drained.
    assert sched.tick(now=base + 10) == []


def test_wakeup_scheduler_cancel():
    sched = WakeupScheduler()
    wid = sched.schedule("later", 5)
    assert sched.cancel(wid) is True
    assert sched.cancel(wid) is False
    assert sched.list() == []


def test_wakeup_scheduler_list_shows_pending():
    sched = WakeupScheduler()
    sched.schedule("a", 10)
    sched.schedule("b", 20)
    pending = sched.list()
    assert len(pending) == 2
    assert {w.prompt for w in pending} == {"a", "b"}


def test_wakeup_scheduler_next_deadline():
    sched = WakeupScheduler()
    assert sched.next_deadline() is None
    sched.schedule("a", 5)
    sched.schedule("b", 2)
    # Earliest deadline should be ~2s out.
    delta = sched.next_deadline() - time.monotonic()
    assert 1 < delta < 3


def test_wakeup_scheduler_min_delay_clamped():
    """delay < 1 is clamped to 1."""
    sched = WakeupScheduler()
    wid = sched.schedule("quick", 0.01)
    pending = sched.list()
    assert len(pending) == 1
    delta = pending[0].fire_at - time.monotonic()
    assert delta >= 0.9  # at least 1 second


# ── schedule_wakeup tool ─────────────────────────────────────────────────

def test_schedule_wakeup_tool_requires_scheduler(ctx):
    """Without a project scheduler, returns a clear error."""
    tools = dispatch(builtin_tools())
    out = tools["schedule_wakeup"].handle(
        ctx, {"delaySeconds": 5, "prompt": "wake"})
    assert "Error" in out
    assert "WakeupScheduler" in out


def test_schedule_wakeup_tool_schedules(ctx):
    """With scheduler attached, returns wakeup_id."""
    @dataclass
    class _P:
        wakeups: WakeupScheduler = None
    p = _P(wakeups=WakeupScheduler())
    ctx.project_ref = p
    tools = dispatch(builtin_tools())
    out = tools["schedule_wakeup"].handle(
        ctx, {"delaySeconds": 5, "prompt": "wake up", "reason": "test"})
    assert "[Wakeup" in out
    wid = out.split("Wakeup ")[1].split(" ")[0]
    assert p.wakeups.list()[0].wakeup_id == wid
    assert p.wakeups.list()[0].prompt == "wake up"


def test_schedule_wakeup_tool_rejects_too_long(ctx):
    """delaySeconds > 3600 is refused."""
    @dataclass
    class _P:
        wakeups: WakeupScheduler = None
    p = _P(wakeups=WakeupScheduler())
    ctx.project_ref = p
    tools = dispatch(builtin_tools())
    out = tools["schedule_wakeup"].handle(
        ctx, {"delaySeconds": 99999, "prompt": "x"})
    assert "Error" in out
    assert "exceeds cap" in out


def test_list_wakeups_tool(ctx):
    @dataclass
    class _P:
        wakeups: WakeupScheduler = None
    p = _P(wakeups=WakeupScheduler())
    p.wakeups.schedule("first", 5)
    p.wakeups.schedule("second", 10)
    ctx.project_ref = p
    tools = dispatch(builtin_tools())
    out = tools["list_wakeups"].handle(ctx, {})
    assert "first" in out
    assert "second" in out
    assert "Pending wakeups (2)" in out


def test_cancel_wakeup_tool(ctx):
    @dataclass
    class _P:
        wakeups: WakeupScheduler = None
    p = _P(wakeups=WakeupScheduler())
    wid = p.wakeups.schedule("doomed", 5)
    ctx.project_ref = p
    tools = dispatch(builtin_tools())
    out = tools["cancel_wakeup"].handle(ctx, {"wakeup_id": wid})
    assert "Cancelled" in out
    assert p.wakeups.list() == []


def test_cancel_wakeup_unknown(ctx):
    @dataclass
    class _P:
        wakeups: WakeupScheduler = None
    p = _P(wakeups=WakeupScheduler())
    ctx.project_ref = p
    tools = dispatch(builtin_tools())
    out = tools["cancel_wakeup"].handle(ctx, {"wakeup_id": "wakeup_nope"})
    assert "Error" in out


# ── /loop /config /output-style slash commands ───────────────────────────

def _run_cmd(name, *, project=None, args=""):
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None
    ctx = CommandContext(
        project_id="proj-g", session_id="sess-g",
        tenant_id="tenant-g", args=args, project=project,
    )
    return list(cmd.handler(ctx))


def test_slash_commands_registered():
    reg = default_registry()
    for n in ("loop", "config", "output-style"):
        assert reg.resolve(n) is not None


def test_loop_command_empty():
    events = _run_cmd("loop", project=None)
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "no scheduled jobs" in text.lower() or "no active" in text.lower()


def test_loop_command_lists_jobs():
    """Migrated in Plan B.6 — /loop now emits a unified list card."""
    from mini_cc.scheduler import WakeupScheduler
    sched_w = WakeupScheduler()
    sched_w.schedule("wakeup task", 5)

    class _Cron:
        def list_jobs(self):
            class J:
                job_id = "cron_1"
                cron = "*/5 * * * *"
                recurring = True
                durable = True
                prompt = "every 5 min"
            return [J()]
    class _P:
        scheduler = _Cron()
        wakeups = sched_w
    events = _run_cmd("loop", project=_P())
    card = next(e for e in events if e.get("type") == "card")
    blob = repr(card["payload"])
    assert "cron_1" in blob
    assert "every 5 min" in blob
    assert "wakeup task" in blob


def test_config_command_redacts_api_key():
    """Config command must redact API keys.

    Since the rich-card migration /config emits a key_value card event
    instead of a text blob. The redaction invariant is the same: the
    full secret must never appear in the wire payload (the value field
    is what the frontend renders, masked or not).
    """
    set_default_config(AnthropicConfig(
        api_key="sk-abc-1234567890",
        tavily_api_key="tvly-abcdefghij",
        primary_model="claude-sonnet-4-6",
    ))
    events = _run_cmd("config")
    card = next(e for e in events if e.get("type") == "card")
    pairs = {p["k"]: p for p in card["payload"]["pairs"]}
    # Full key must never appear anywhere in the card payload.
    blob = repr(card)
    assert "sk-abc-1234567890" not in blob
    assert "tvly-abcdefghij" not in blob
    # API keys are flagged sensitive so the frontend masks them by
    # default; the value is the pre-redacted short form.
    assert pairs["anthropic_api_key"]["sensitive"] is True
    # _redact shows first 4 + •••• + last 4
    assert pairs["anthropic_api_key"]["v"].endswith("7890")
    assert "••••" in pairs["anthropic_api_key"]["v"]
    # Model is rendered as a non-sensitive mono value.
    assert pairs["primary_model"]["v"] == "claude-sonnet-4-6"
    assert pairs["primary_model"]["sensitive"] is False


def test_config_command_shows_unset():
    """When nothing is configured, the card still renders and uses the
    em-dash placeholder for unset values."""
    set_default_config(AnthropicConfig())
    events = _run_cmd("config")
    card = next(e for e in events if e.get("type") == "card")
    pair_keys = {p["k"] for p in card["payload"]["pairs"]}
    assert "primary_model" in pair_keys


def test_output_style_command_requires_warm_session():
    """Without a warm session, /output-style errors cleanly."""
    events = _run_cmd("output-style", project=None, args="terse")
    # Either an error event or text mentioning session not warm.
    has_error = any(e.get("type") == "error" for e in events)
    assert has_error


def test_output_style_command_sets_style():
    """With a warm session, /output-style terse sets state.output_style."""
    class _State:
        output_style = ""
    class _Loop:
        state = _State()
    class _Sess:
        loop = _Loop()
    class _SM:
        _sessions = {("proj-g", "sess-g"): _Sess()}
    class _Project:
        pass

    # The /output-style handler reaches into sm._sessions; we need
    # session_manager on the CommandContext.
    reg = default_registry()
    cmd = reg.resolve("output-style")
    ctx = CommandContext(
        project_id="proj-g", session_id="sess-g",
        tenant_id="t-g", args="terse",
        project=_Project(), session_manager=_SM(),
    )
    events = list(cmd.handler(ctx))
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "terse" in text
    assert _Loop.state.output_style == "terse"


def test_output_style_command_rejects_unknown_style():
    class _State:
        output_style = ""
    class _Loop:
        state = _State()
    class _Sess:
        loop = _Loop()
    class _SM:
        _sessions = {("proj-g", "sess-g"): _Sess()}
    reg = default_registry()
    cmd = reg.resolve("output-style")
    ctx = CommandContext(
        project_id="proj-g", session_id="sess-g",
        tenant_id="t-g", args="verbose-please",
        project=type("P", (), {}), session_manager=_SM(),
    )
    events = list(cmd.handler(ctx))
    err = next(e for e in events if e.get("type") == "error")
    assert "unknown" in err["message"].lower()


# ── LSP ──────────────────────────────────────────────────────────────────

def test_lsp_language_for_file():
    from mini_cc.lsp import LSPManager
    assert LSPManager.language_for_file("foo.py") == "python"
    assert LSPManager.language_for_file("bar.ts") == "typescript"
    assert LSPManager.language_for_file("baz.rs") == "rust"
    assert LSPManager.language_for_file("x.unknown") is None


def test_lsp_tool_rejects_unknown_operation(ctx):
    tools = dispatch(builtin_tools())
    out = tools["lsp"].handle(ctx, {"operation": "frobnicate",
                                    "filePath": "x.py"})
    assert "Error" in out
    assert "frobnicate" in out


def test_lsp_tool_requires_file_path(ctx):
    tools = dispatch(builtin_tools())
    out = tools["lsp"].handle(ctx, {"operation": "hover"})
    assert "Error" in out
    assert "filePath" in out


def test_lsp_tool_requires_language(ctx, sandbox):
    """A file with unknown extension must prompt for language."""
    (sandbox.project_root / "x.unknownext").write_text("hello")
    tools = dispatch(builtin_tools())
    out = tools["lsp"].handle(ctx, {"operation": "hover",
                                    "filePath": "x.unknownext"})
    assert "Error" in out
    assert "language" in out.lower()


def test_lsp_tool_requires_manager(ctx):
    """Without a manager attached, error is clear."""
    (ctx.sandbox.project_root / "x.py").write_text("print('hi')")
    tools = dispatch(builtin_tools())
    out = tools["lsp"].handle(ctx, {"operation": "hover",
                                    "filePath": "x.py",
                                    "line": 0, "character": 0})
    assert "Error" in out
    assert "LSPManager" in out


def test_lsp_tool_dispatches_to_manager(ctx, sandbox):
    """With a manager stubbed, the tool formats the response."""

    class _FakeManager:
        def __init__(self):
            self.last_call = None
        def request(self, language, method, params, *, timeout=10.0):
            self.last_call = (language, method, params)
            if method == "textDocument/hover":
                return {"contents": {"kind": "markdown",
                                     "value": "Function `foo(x: int) -> str`"}}
            if method == "textDocument/definition":
                return [{"uri": "file:///x.py", "range": {
                    "start": {"line": 10, "character": 5},
                    "end": {"line": 10, "character": 8}}}]

    class _P:
        lsp_manager = _FakeManager()
    ctx.project_ref = _P
    (sandbox.project_root / "x.py").write_text("print('hi')\n")

    tools = dispatch(builtin_tools())
    out = tools["lsp"].handle(ctx, {"operation": "hover",
                                    "filePath": "x.py",
                                    "line": 0, "character": 1})
    assert "foo" in out
    assert _P.lsp_manager.last_call[0] == "python"
    assert _P.lsp_manager.last_call[1] == "textDocument/hover"


def test_lsp_format_result_handles_none():
    """A None LSP response surfaces as _no result_."""
    from mini_cc.lsp import _format_result
    out = _format_result("hover", None)
    assert "no result" in out.lower()


def test_lsp_format_result_hover_dict():
    from mini_cc.lsp import _format_result
    out = _format_result("hover", {
        "contents": {"kind": "markdown", "value": "docstring here"}})
    assert "docstring here" in out


def test_lsp_format_result_definition_list():
    from mini_cc.lsp import _format_result
    out = _format_result("goToDefinition", [
        {"uri": "file:///foo.py",
         "range": {"start": {"line": 12, "character": 3}}}
    ])
    assert "foo.py" in out
    assert "12:3" in out


# ── Workflow: parsing ────────────────────────────────────────────────────

def test_workflow_from_dict_basic():
    wf = workflow_from_dict({
        "name": "research",
        "steps": [
            {"id": "find", "prompt": "find {topic}"},
            {"id": "summarize", "prompt": "summarize {find}",
             "condition": "find"},
        ],
    })
    assert wf.name == "research"
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "find"
    assert wf.steps[1].condition == "find"


def test_workflow_from_markdown_basic():
    md = """---
name: md-test
description: A workflow from markdown
---
## step_a
Do thing A.

## step_b
Do thing B using {step_a}.
"""
    wf = workflow_from_markdown(md)
    assert wf.name == "md-test"
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "step_a"
    assert "Do thing A" in wf.steps[0].prompt
    assert wf.steps[1].prompt.startswith("Do thing B")


def test_workflow_add_step_dynamic():
    wf = workflow_from_dict({"name": "x", "steps": []})
    assert wf.steps == []
    wf.add_step(WorkflowStep(id="s1", prompt="hello"))
    assert len(wf.steps) == 1
    assert wf.steps[0].id == "s1"


# ── Workflow: runner ─────────────────────────────────────────────────────

def test_workflow_runner_executes_in_order():
    """Steps run in declared order; results feed into next step's prompt."""
    calls: list[str] = []
    def dispatcher(prompt: str) -> str:
        calls.append(prompt)
        return f"result_for({prompt.split()[-1]})"

    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "do A"},
            {"id": "b", "prompt": "then B with {a}"},
        ],
    })
    events = list(run_workflow(wf, dispatcher))
    # 1 start + 2 × (step_started, step_completed) + 1 complete
    assert events[0]["type"] == "workflow_started"
    assert events[-1]["type"] == "workflow_completed"
    types = [e["type"] for e in events]
    assert types.count("step_started") == 2
    assert types.count("step_completed") == 2
    # Second call should have substituted {a} with the first result.
    assert "result_for(A)" in calls[1]
    # Workflow state carries results.
    assert wf.results["a"].startswith("result_for")
    assert wf.results["b"].startswith("result_for")
    assert wf.status == "completed"


def test_workflow_runner_skips_false_condition():
    """A condition that evaluates to false → step_skipped, no dispatch."""
    calls: list[str] = []
    def dispatcher(p: str) -> str:
        calls.append(p)
        return "ok"
    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "do A"},
            # `not_a` is empty/falsy → condition fails
            {"id": "b", "prompt": "do B", "condition": "not_a"},
        ],
    })
    events = list(run_workflow(wf, dispatcher))
    types = [e["type"] for e in events]
    assert "step_skipped" in types
    # b was skipped, only a was dispatched.
    assert len(calls) == 1
    assert "do A" in calls[0]


def test_workflow_runner_runs_conditional_step_when_truthy():
    """A condition referencing a prior result works."""
    def dispatcher(p: str) -> str:
        return "non-empty"
    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "do A"},
            # `a` in scope is the result string "non-empty" → truthy
            {"id": "b", "prompt": "do B", "condition": "a"},
        ],
    })
    events = list(run_workflow(wf, dispatcher))
    types = [e["type"] for e in events]
    assert types.count("step_completed") == 2


def test_workflow_runner_on_failure_abort():
    """A failing step with on_failure=abort halts the workflow."""
    def dispatcher(p: str) -> str:
        raise RuntimeError("boom")
    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "do A", "on_failure": "abort"},
            {"id": "b", "prompt": "do B"},
        ],
    })
    events = list(run_workflow(wf, dispatcher))
    types = [e["type"] for e in events]
    assert "step_failed" in types
    assert "workflow_aborted" in types
    assert wf.status == "aborted"
    # Step b never ran.
    assert "b" not in wf.results


def test_workflow_runner_on_failure_skip():
    """A failing step with on_failure=skip continues to next step."""
    seq: list[str] = []
    def dispatcher(p: str) -> str:
        seq.append(p)
        if p == "do A":
            raise RuntimeError("boom")
        return "ok"
    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "do A", "on_failure": "skip"},
            {"id": "b", "prompt": "do B"},
        ],
    })
    events = list(run_workflow(wf, dispatcher))
    types = [e["type"] for e in events]
    assert "step_failed" in types
    assert "workflow_completed" in types
    # B was attempted despite A failing.
    assert "do B" in seq


def test_workflow_runner_substitutes_initial_state():
    """initial_state provides placeholder values."""
    def dispatcher(p: str) -> str:
        return f"ran:{p}"
    wf = workflow_from_dict({
        "name": "t",
        "steps": [
            {"id": "a", "prompt": "topic is {topic}"},
        ],
    })
    list(run_workflow(wf, dispatcher, initial_state={"topic": "Python"}))
    assert wf.results["a"] == "ran:topic is Python"


# ── Workflow tools ───────────────────────────────────────────────────────

def test_workflow_create_from_dict(ctx):
    class _P: pass
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_create"].handle(ctx, {
        "name": "wf-test",
        "workflow": {
            "steps": [
                {"id": "a", "prompt": "do A"},
                {"id": "b", "prompt": "do B"},
            ],
        },
    })
    assert "workflow created" in out.lower()
    assert ctx.project_ref.active_workflow.name == "wf-test"


def test_workflow_create_from_markdown(ctx):
    class _P: pass
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_create"].handle(ctx, {
        "markdown": "## a\nDo A\n## b\nDo B\n",
    })
    assert "workflow created" in out.lower()
    wf = ctx.project_ref.active_workflow
    assert len(wf.steps) == 2


def test_workflow_status_no_active(ctx):
    class _P: pass
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_status"].handle(ctx, {})
    assert "no active" in out.lower()


def test_workflow_status_shows_active(ctx):
    class _P:
        active_workflow = workflow_from_dict({
            "name": "demo",
            "steps": [{"id": "a", "prompt": "A"},
                      {"id": "b", "prompt": "B"}],
        })
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_status"].handle(ctx, {})
    assert "demo" in out
    assert "`a`" in out
    assert "`b`" in out


def test_workflow_add_step(ctx):
    class _P:
        active_workflow = workflow_from_dict({"name": "x", "steps": []})
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_add_step"].handle(ctx, {
        "id": "s1", "prompt": "do thing",
    })
    assert "added step" in out.lower()
    assert len(_P.active_workflow.steps) == 1


def test_workflow_add_step_requires_active(ctx):
    class _P: pass
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_add_step"].handle(ctx, {
        "id": "s1", "prompt": "x",
    })
    assert "Error" in out


def test_workflow_set_state(ctx):
    class _P:
        active_workflow = workflow_from_dict({"name": "x", "steps": []})
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_set_state"].handle(ctx, {
        "state": {"topic": "Python", "depth": 3},
    })
    assert "state updated" in out.lower()
    assert _P.active_workflow.state["topic"] == "Python"


def test_workflow_run_step_with_dispatcher(ctx):
    """workflow_dispatch closure drives execution."""
    class _P:
        active_workflow = workflow_from_dict({
            "name": "x",
            "steps": [{"id": "a", "prompt": "hello"}],
        })
    ctx.project_ref = _P
    ctx.workflow_dispatch = lambda p: f"DISPATCHED[{p}]"   # type: ignore
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_step"].handle(ctx, {"id": "a"})
    assert "DISPATCHED[hello]" in out
    assert _P.active_workflow.results["a"].startswith("DISPATCHED")


def test_workflow_run_step_unknown(ctx):
    class _P:
        active_workflow = workflow_from_dict({
            "name": "x", "steps": [{"id": "a", "prompt": "A"}],
        })
    ctx.project_ref = _P
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_step"].handle(ctx, {"id": "nope"})
    assert "Error" in out
    assert "nope" in out


def test_workflow_run_step_condition_false_skips(ctx):
    class _P:
        active_workflow = workflow_from_dict({
            "name": "x",
            "steps": [
                {"id": "a", "prompt": "A"},
                {"id": "b", "prompt": "B", "condition": "never_set"},
            ],
        })
    ctx.project_ref = _P
    ctx.workflow_dispatch = lambda p: f"did[{p}]"  # type: ignore
    tools = dispatch(builtin_tools())
    out = tools["workflow_run_step"].handle(ctx, {"id": "b"})
    assert "skipped" in out.lower()
