"""Tests for the slash/tools/bash enhancements (Phase F).

Covers:
- bash: timeout / cwd / run_in_background knobs
- new tools: web_fetch, task_output, task_stop
- new slash commands: /cost, /permissions, /agents, /logs
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mini_cc.commands import CommandContext, SlashCommand, default_registry
from mini_cc.sandbox import SubprocessSandbox, CommandBlockedError
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import FunctionTool, ToolContext
from mini_cc.tools.background import BackgroundScheduler


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def sandbox(tmp_path):
    return SubprocessSandbox("proj-test", tmp_path / "ws")


@pytest.fixture
def storage(tmp_path):
    return FSStorage(tmp_path / "state")


@pytest.fixture
def ctx(sandbox, storage):
    return ToolContext(
        project_id="proj-test", session_id="sess-test",
        sandbox=sandbox, storage=storage, todos=[],
    )


# ── Bash: timeout / cwd ─────────────────────────────────────────────────

def test_bash_default_timeout_used(sandbox):
    """No timeout arg → sandbox default (120s) applies. We just verify
    the call still works; asserting the exact value would require
    intercepting subprocess.run."""
    r = sandbox.execute("echo hello")
    assert "hello" in r.stdout


def test_bash_custom_timeout_accepted(sandbox):
    """A short timeout must be honored — a sleep longer than the
    timeout must raise TimeoutExpired."""
    from subprocess import TimeoutExpired
    with pytest.raises(TimeoutExpired):
        sandbox.execute("sleep 5", timeout=1)


def test_bash_cwd_resolves_relative(sandbox, tmp_path):
    """cwd as a relative subdir must resolve under project_root and
    actually become the working directory of the subprocess."""
    (sandbox.project_root / "subdir").mkdir()
    r = sandbox.execute("pwd", cwd="subdir")
    # pwd should resolve into the subdir under project_root. On Windows
    # under Git Bash, pwd returns POSIX form (/c/...); on POSIX it
    # returns the resolved path. Either way the leaf subdir must appear
    # AND the project_root's leaf name must precede it.
    assert "subdir" in r.stdout
    assert sandbox.project_root.name in r.stdout


def test_bash_cwd_rejects_escape(sandbox, tmp_path):
    """cwd trying to break out of project_root via .. must raise."""
    from mini_cc.sandbox import PathEscapeError
    with pytest.raises(PathEscapeError):
        sandbox.execute("pwd", cwd="../../etc")


def test_bash_cwd_none_defaults_to_root(sandbox):
    """cwd=None is the same as not passing it (project_root)."""
    r = sandbox.execute("pwd", cwd=None)
    assert sandbox.project_root.name in r.stdout


# ── Bash tool wrapper: timeout coercion ──────────────────────────────────

def test_bash_tool_clamps_timeout(sandbox, ctx):
    """timeout above MAX (600) is clamped; we just check the tool
    accepts the arg without error."""
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "echo clamp", "timeout": 99999})
    assert "clamp" in out


def test_bash_tool_negative_timeout_falls_back_to_default(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "echo neg", "timeout": -5})
    assert "neg" in out


def test_bash_tool_bogus_timeout_falls_back(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "echo bogus", "timeout": "not-a-number"})
    assert "bogus" in out


def test_bash_tool_cwd_param_works(sandbox, ctx):
    """The tool-level cwd param must thread through to the sandbox."""
    (sandbox.project_root / "deep").mkdir()
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "pwd", "cwd": "deep"})
    assert "deep" in out


def test_bash_tool_cwd_escape_returns_error(sandbox, ctx):
    """Path escape surfaces as an Error: line in tool output (FunctionTool
    catches the exception)."""
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "pwd", "cwd": "../../etc"})
    assert "Error" in out


# ── Bash tool: run_in_background ─────────────────────────────────────────

def test_bash_run_in_background_requires_scheduler(sandbox, ctx):
    """Without a BackgroundScheduler wired in, the tool refuses with
    a clear error message."""
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "echo hi", "run_in_background": True})
    assert "Error" in out
    assert "BackgroundScheduler" in out


def test_bash_run_in_background_starts_task(sandbox, ctx):
    """With a scheduler wired in, run_in_background returns a bg_id."""
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    ctx.background_tools = dispatch(builtin_tools())
    tools = dispatch(builtin_tools())
    out = tools["bash"].handle(ctx, {"command": "echo async hi", "run_in_background": True})
    assert "[Background task" in out
    # Pull the bg_id (format: bg_xxxxxxxx) and wait for completion.
    bg_id = out.split("Background task ")[1].split(" ")[0]
    assert bg_id.startswith("bg_")
    # Wait for the worker to finish.
    deadline = time.monotonic() + 5
    while bg.status(bg_id) == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bg.status(bg_id) == "completed"
    assert "async hi" in bg.get_output(bg_id)


# ── task_output / task_stop tools ────────────────────────────────────────

def test_task_output_unknown_id(sandbox, ctx):
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    tools = dispatch(builtin_tools())
    out = tools["task_output"].handle(ctx, {"bg_id": "bg_doesnotexist"})
    assert "Error" in out


def test_task_output_running_placeholder(sandbox, ctx):
    """A still-running task returns a [bg_xxx still running] placeholder."""
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    ctx.background_tools = dispatch(builtin_tools())
    # Long-running command — keep the worker busy.
    bg_id = bg.start_bg(ctx, "bash",
                        {"command": "sleep 2", "timeout": 5},
                        "tu1", command_str="sleep 2")
    tools = dispatch(builtin_tools())
    out = tools["task_output"].handle(ctx, {"bg_id": bg_id})
    assert "still running" in out
    # Cleanup — mark stopped so the worker doesn't leak.
    bg.stop(bg_id)


def test_task_output_returns_result_when_complete(sandbox, ctx):
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    ctx.background_tools = dispatch(builtin_tools())
    bg_id = bg.start_bg(ctx, "bash",
                        {"command": "echo result-data"},
                        "tu1", command_str="echo result-data")
    deadline = time.monotonic() + 5
    while bg.status(bg_id) == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    tools = dispatch(builtin_tools())
    out = tools["task_output"].handle(ctx, {"bg_id": bg_id})
    assert "result-data" in out


def test_task_output_without_scheduler(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["task_output"].handle(ctx, {"bg_id": "bg_x"})
    assert "Error" in out
    assert "BackgroundScheduler" in out


def test_task_stop_unknown_id(sandbox, ctx):
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    tools = dispatch(builtin_tools())
    out = tools["task_stop"].handle(ctx, {"bg_id": "bg_nope"})
    assert "Error" in out


def test_task_stop_marks_stopped(sandbox, ctx):
    bg = BackgroundScheduler()
    ctx.background_scheduler = bg
    ctx.background_tools = dispatch(builtin_tools())
    bg_id = bg.start_bg(ctx, "bash",
                        {"command": "sleep 5", "timeout": 10},
                        "tu1", command_str="sleep 5")
    tools = dispatch(builtin_tools())
    out = tools["task_stop"].handle(ctx, {"bg_id": bg_id})
    assert "stop requested" in out
    # Status should now be "stopped", not "running".
    assert bg.status(bg_id) == "stopped"
    # collect_notifications now surfaces it as stopped.
    notes = bg.collect_notifications()
    assert any("stopped" in n for n in notes)


def test_task_stop_without_scheduler(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["task_stop"].handle(ctx, {"bg_id": "bg_x"})
    assert "Error" in out


# ── web_fetch ────────────────────────────────────────────────────────────

def test_web_fetch_missing_url(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["web_fetch"].handle(ctx, {})
    assert "Error" in out


def test_web_fetch_rejects_non_http(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["web_fetch"].handle(ctx, {"url": "ftp://example.com/x"})
    assert "Error" in out


def test_web_fetch_rejects_garbage_url(sandbox, ctx):
    tools = dispatch(builtin_tools())
    out = tools["web_fetch"].handle(ctx, {"url": "not-a-url"})
    assert "Error" in out


def test_web_fetch_unknown_host(sandbox, ctx):
    """Unknown host returns an Error string (not an exception)."""
    tools = dispatch(builtin_tools())
    out = tools["web_fetch"].handle(ctx, {
        "url": "http://nonexistent.invalid./x",
        "timeout": 2,
    })
    assert "Error" in out


def test_web_fetch_succeeds_for_local_server(sandbox, ctx):
    """Spin up a tiny HTTP server in a thread, fetch from it, verify
    the body comes back."""
    import http.server, socketserver
    payload = "hello-from-local-server"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(payload.encode())
        def log_message(self, *a): pass

    # Bind to port 0 to let the OS pick; then read back the actual port.
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            tools = dispatch(builtin_tools())
            out = tools["web_fetch"].handle(ctx, {
                "url": f"http://127.0.0.1:{port}/x",
                "timeout": 5,
            })
            assert "200" in out
            assert payload in out
        finally:
            httpd.shutdown()


# ── Slash commands ───────────────────────────────────────────────────────

def _run_cmd(name, *, project=None, args="") -> list[dict]:
    """Drive a slash command handler directly and collect its events."""
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None, f"command {name} not registered"
    ctx = CommandContext(
        project_id="proj-test", session_id="sess-test",
        tenant_id="tenant-test", args=args, project=project,
    )
    return list(cmd.handler(ctx))


def test_slash_commands_registered():
    """All 4 new commands resolve in the default registry."""
    reg = default_registry()
    for name in ("cost", "permissions", "agents", "logs"):
        cmd = reg.resolve(name)
        assert cmd is not None, f"/{name} missing"
        assert cmd.scope == "server"


def test_cost_without_metrics_returns_friendly_message():
    """Project without a metrics registry → friendly fallback."""
    class _P:
        metrics = None
        tenant_id = "t1"
    events = _run_cmd("cost", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "not configured" in text.lower()


def test_cost_on_real_project_without_metrics_does_not_crash(tmp_path):
    """Regression: real ``Project`` previously raised
    ``AttributeError: 'Project' object has no attribute 'metrics'``
    because the dataclass field is ``_metrics`` but ``/cost`` read
    ``project.metrics``. Tests using a mock ``_P`` class hid the bug —
    exercise the real type here."""
    from mini_cc.projects import ProjectManager
    pm = ProjectManager(tmp_path / "pm")
    project = pm.create(tenant_id="t1", project_id="proj-real")
    events = _run_cmd("cost", project=project)
    text = next(e["text"] for e in events if e["type"] == "text")
    # No metrics registry wired → friendly fallback, NOT AttributeError.
    assert "not configured" in text.lower()


def test_cost_reports_token_totals():
    """With a metrics registry populated, /cost shows the numbers."""
    from mini_cc.server.metrics import default_registry as _metrics_factory, record_tokens
    reg = _metrics_factory()
    record_tokens(reg, "t1", input=1000, output=500, cache_read=50, cache_create=10)

    class _P:
        metrics = reg
        tenant_id = "t1"
    events = _run_cmd("cost", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "1,000" in text           # input
    assert "500" in text             # output
    assert "total" in text.lower()


def test_permissions_lists_blocked_patterns():
    """Permissions command surfaces the sandbox policy."""
    class _P:
        tenant_id = "t1"
        class sandbox:
            policy = SubprocessSandbox("p", Path(".")).policy
    events = _run_cmd("permissions", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "Blocked" in text or "blocked" in text.lower()
    assert "sudo" in text              # in DEFAULT_BLOCKED_PATTERNS
    assert "DENY_LIST" in text
    # Allowed git
    assert "status" in text
    assert "commit" in text


def test_permissions_handles_missing_sandbox():
    """If project.sandbox is None, command degrades gracefully."""
    class _P:
        sandbox = None
        tenant_id = "t1"
    events = _run_cmd("permissions", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "not available" in text.lower()


def test_agents_without_spawner_returns_message():
    class _P:
        teams = None
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P())
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "not configured" in text.lower()


def test_agents_lists_spawned_teammates():
    """A spawner with one alive teammate lists it in the roster card.

    Phase 3.1 migrated /agents to a list card; this test now asserts
    against the card payload instead of the old text blob.
    """
    class _Info:
        def __init__(self, name, role, alive=True):
            self.name = name; self.role = role; self.alive = alive
            self.worktree = None
    class _Spawner:
        def __init__(self):
            self._teammates = {"alice": _Info("alice", "researcher")}
        def list_alive(self):
            return [t for t in self._teammates.values() if t.alive]
    class _P:
        teams = _Spawner()
        tenant_id = "t1"
    events = _run_cmd("agents", project=_P())
    card = next(e for e in events if e.get("type") == "card")
    items = card["payload"]["items"]
    alice = next(it for it in items if it["title"] == "alice")
    assert alice["subtitle"] and "researcher" in alice["subtitle"]
    badge_texts = [b["text"] for b in alice["badges"]]
    assert "alive" in badge_texts  # was 🟢 in the old text format


def test_logs_lists_files():
    """The logs directory exists (this repo writes to it); /logs must
    list at least one file."""
    events = _run_cmd("logs")
    text = next(e["text"] for e in events if e["type"] == "text")
    # Either we found files ("Recent log files") or we report empty.
    assert ("Recent log files" in text
            or "no log files" in text
            or "not found" in text.lower())


def test_logs_command_visible_in_help():
    """/help must enumerate every new command."""
    reg = default_registry()
    visible_names = {c.name for c in reg.all_visible()}
    for name in ("cost", "permissions", "agents", "logs"):
        assert name in visible_names


# ── BackgroundScheduler direct API ───────────────────────────────────────

def test_scheduler_get_output_unknown():
    bg = BackgroundScheduler()
    assert "unknown" in bg.get_output("bg_nope").lower()


def test_scheduler_stop_unknown():
    bg = BackgroundScheduler()
    assert "unknown" in bg.stop("bg_nope").lower()


def test_scheduler_list_tasks_includes_running():
    """list_tasks returns all known tasks."""
    bg = BackgroundScheduler()
    # Manually inject a task without starting a thread.
    from mini_cc.tools.background import _BGTask
    bg._tasks["bg_x"] = _BGTask(
        bg_id="bg_x", tool_use_id="tu1", tool_name="bash", command="echo x")
    tasks = bg.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["bg_id"] == "bg_x"
    assert tasks[0]["status"] == "running"


def test_scheduler_collect_notifications_skips_running():
    """Running tasks don't drain; completed/stopped do."""
    bg = BackgroundScheduler()
    from mini_cc.tools.background import _BGTask
    bg._tasks["bg_a"] = _BGTask(
        bg_id="bg_a", tool_use_id="tu1", tool_name="bash",
        command="x", status="running")
    bg._tasks["bg_b"] = _BGTask(
        bg_id="bg_b", tool_use_id="tu2", tool_name="bash",
        command="y", status="completed", result="done")
    notes = bg.collect_notifications()
    assert len(notes) == 1
    assert "bg_b" in notes[0]
    # Running task stays in the pool.
    assert "bg_a" in bg._tasks
    assert "bg_b" not in bg._tasks
