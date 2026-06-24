"""Code-execution tool: stateful Python REPL + one-shot JS/Shell.

Python state survives across calls within the same session (variables,
imports). JS/Node and Shell run stateless one-shot — sandbox.execute
already serves them well.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.sandbox import Policy, SubprocessSandbox
from mini_cc.tools import FunctionTool, ToolContext
from mini_cc.tools.repl import (EXEC_CODE_TOOL, _build_python_wrapper,
                                 _state_file_path)


def _ctx(tmp_path, sandbox=None, session_id="s1"):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sb = sandbox or SubprocessSandbox("p1", ws, Policy())
    return ToolContext(
        project_id="p1", session_id=session_id, sandbox=sb,
        storage=None, todos=[],  # type: ignore[arg-type]
    ), ws


# ── Pure helpers ──────────────────────────────────────────────────────

def test_state_file_path_is_workspace_scoped(tmp_path):
    sf = _state_file_path(tmp_path / "ws", "s1", "python")
    assert sf == (tmp_path / "ws" / ".mini_cc" / "repl" / "s1.python.pickle")


def test_build_python_wrapper_loads_and_saves_state(tmp_path):
    state_file = tmp_path / "state.pkl"
    wrapper = _build_python_wrapper(
        user_code="x = 10\nprint('x is', x)",
        state_file=state_file)
    # Wrapper uses forward-slash form (POSIX-safe inside container).
    normalized = str(state_file).replace("\\", "/")
    assert normalized in wrapper
    assert "_pkl.load" in wrapper
    assert "_pkl.dump" in wrapper
    # User code is base64-encoded, not inlined raw.
    assert "x = 10" not in wrapper
    assert "base64" in wrapper


# ── Python stateful execution ─────────────────────────────────────────

def test_python_stateless_first_call(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "print(2 + 3)",
    })
    assert "5" in r


def test_python_state_persists_across_calls(tmp_path):
    ctx, _ = _ctx(tmp_path)
    EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "x = 42\nimport math\nconst = math.pi",
    })
    r2 = EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "print(x * 2)\nprint(round(const, 2))",
    })
    # x*2 == 84, round(pi, 2) == 3.14
    assert "84" in r2
    assert "3.14" in r2


def test_python_reset_clears_state(tmp_path):
    ctx, _ = _ctx(tmp_path)
    EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "x = 999",
    })
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "print(x)",
        "reset": True,
    })
    # After reset, x is no longer in scope → NameError surfaces.
    assert "999" not in r
    assert "Error" in r or "NameError" in r or "not defined" in r


def test_python_stderr_surfaced(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "import sys\nprint('boom', file=sys.stderr)",
    })
    assert "boom" in r


def test_python_state_isolated_per_session(tmp_path):
    """Two sessions under the same project must not share Python state."""
    ctx_a, _ = _ctx(tmp_path, session_id="s_a")
    ctx_b, _ = _ctx(tmp_path, session_id="s_b")
    EXEC_CODE_TOOL.handle(ctx_a, {
        "language": "python",
        "code": "secret = 'from-a'",
    })
    r = EXEC_CODE_TOOL.handle(ctx_b, {
        "language": "python",
        "code": "print(secret)",
    })
    assert "from-a" not in r
    assert "Error" in r or "not defined" in r


def test_python_unpicklable_filtered(tmp_path):
    """Open file handles can't pickle — wrapper must skip them rather
    than blow up the whole state save."""
    ctx, _ = _ctx(tmp_path)
    # Open a file and keep the handle in the namespace. Wrapper should
    # filter it; subsequent call must still see the integer.
    EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "f = open('side.txt', 'w')\nval = 7",
    })
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "python",
        "code": "print(val)",
    })
    assert "7" in r


# ── JS / Node stateless one-shot ──────────────────────────────────────

def test_javascript_runs_when_node_available(tmp_path):
    shutil = pytest.importorskip("shutil")
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "javascript",
        "code": "console.log(6 * 7)",
    })
    assert "42" in r


def test_javascript_missing_node_surfaces_error(tmp_path, monkeypatch):
    monkeypatch.setattr("mini_cc.tools.repl.shutil.which", lambda _: None)
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "javascript",
        "code": "console.log('hi')",
    })
    assert "Error" in r
    assert "node" in r.lower()


# ── Shell stateless ───────────────────────────────────────────────────

def test_shell_runs_one_shot(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "shell",
        "code": "echo hello && pwd",
    })
    assert "hello" in r


# ── Validation ────────────────────────────────────────────────────────

def test_unknown_language_errors(tmp_path):
    ctx, _ = _ctx(tmp_path)
    r = EXEC_CODE_TOOL.handle(ctx, {
        "language": "ruby",
        "code": "puts 'hi'",
    })
    assert "Error" in r
    assert "ruby" in r.lower() or "language" in r.lower()


def test_tool_is_registered_in_builtin_tools():
    from mini_cc.tools import builtin_tools
    names = [t.name for t in builtin_tools()]
    assert "execute_code" in names
