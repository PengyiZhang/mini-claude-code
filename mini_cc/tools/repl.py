"""Code-execution tool — stateful Python REPL + one-shot JS/Shell.

Python state survives across calls within a session: variables, imports,
even mutated module objects are pickled to a per-session state file under
``<workspace>/.mini_cc/repl/<session>.python.pickle`` and reloaded on
the next call. Unpicklable values (file handles, locks, sockets, custom
objects whose ``__reduce__`` blows up) are silently filtered out — the
agent sees them disappear from namespace but the call itself doesn't fail.

JS/Node and Shell are intentionally stateless one-shot invocations. The
sandbox already supports them well via ``execute()``, and persisting
JS/Shell state across calls is a footgun (interpreter quirks, env-var
inheritance) we'd rather not own.

The handler routes everything through ``ctx.sandbox.execute()``, so the
container sandbox (where execute() lands in ``docker exec``) gets the
code-execution feature transparently — no sandbox-side changes needed.
"""
from __future__ import annotations

import base64
import shutil
import textwrap
from pathlib import Path

from .base import FunctionTool, ToolContext


SUPPORTED_LANGUAGES = ("python", "javascript", "shell")
DEFAULT_TIMEOUT = 60


def _state_file_path(workspace: Path, session_id: str,
                     language: str) -> Path:
    """Per-session, per-language state file under the workspace.

    Placed under .mini_cc/ (the same dir permissions.toml lives in) so a
    workspace wipe naturally clears REPL state."""
    return (workspace / ".mini_cc" / "repl"
            / f"{session_id}.{language}.pickle")


def _build_python_wrapper(*, user_code: str, state_file: Path) -> str:
    """Render the Python wrapper script that:

    1. Loads prior state from ``state_file`` (if any) into a fresh dict.
    2. ``exec()``s the user's code in that namespace.
    3. Writes back any picklable values.

    The wrapper is run via ``sandbox.execute("python -c '<wrapper>'")``
    so it lands inside the container when one is attached.

    Output of ``print()`` from user code goes to stdout normally; the
    wrapper writes nothing to stdout on success so user output is
    uncluttered. On exception the traceback surfaces through stderr.
    """
    # Use base64 so the user code never has to be shell-escaped — a stray
    # quote in the code would otherwise break the python -c invocation.
    encoded = base64.b64encode(user_code.encode("utf-8")).decode("ascii")
    state_str = str(state_file).replace("\\", "/")
    return textwrap.dedent(f"""
        import base64 as _b64, pickle as _pkl, sys as _sys, os as _os, traceback as _tb
        _state_file = {state_str!r}
        _ns = {{}}
        try:
            with open(_state_file, "rb") as _f:
                _ns = _pkl.load(_f)
        except FileNotFoundError:
            pass
        except Exception:
            # Corrupt state file — start fresh, but surface a hint.
            print("REPL: state file unreadable, starting fresh", file=_sys.stderr)
            _ns = {{}}
        # Inject __name__ so `if __name__ == "__main__"` works.
        _ns.setdefault("__name__", "__repl__")
        _user_src = _b64.b64decode({encoded!r}).decode("utf-8")
        try:
            exec(compile(_user_src, "<repl>", "exec"), _ns)
        except Exception:
            _tb.print_exc()
            _sys.exit(1)
        # Best-effort state save: keep only picklable items.
        _saved = {{}}
        for _k, _v in list(_ns.items()):
            if _k.startswith("_") or _k in ("__builtins__", "__name__"):
                continue
            try:
                _pkl.dumps(_v)
                _saved[_k] = _v
            except Exception:
                pass
        try:
            _os.makedirs(_os.path.dirname(_state_file), exist_ok=True)
            with open(_state_file, "wb") as _f:
                _pkl.dump(_saved, _f)
        except Exception as _e:
            print(f"REPL: state save failed: {{_e}}", file=_sys.stderr)
    """)


def _run_python(ctx: ToolContext, code: str, *,
                reset: bool, timeout: int) -> str:
    # OpenSandbox code-interpreter backend: when env opts in and the
    # sandbox is OpenSandbox-backed, use the stateful Jupyter context
    # instead of the local pickle-wrapper path. Falls back silently if
    # the runtime isn't reachable (e.g. sandbox not yet started).
    interp = _get_osb_interp(ctx)
    if interp is not None:
        return interp.run(code, language="python")

    workspace = Path(ctx.sandbox.project_root)  # type: ignore[attr-defined]
    state_file = _state_file_path(workspace, ctx.session_id, "python")
    if reset and state_file.exists():
        try:
            state_file.unlink()
        except OSError:
            pass
    wrapper = _build_python_wrapper(user_code=code, state_file=state_file)
    # Write the wrapper to a temp file in the workspace so we don't fight
    # with shell-quoting on Windows vs POSIX. Using a file also means
    # tracebacks report a meaningful filename.
    script_path = (workspace / ".mini_cc" / "repl"
                   / f"{ctx.session_id}.python.run.py")
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(wrapper, encoding="utf-8")
    rel = script_path.relative_to(workspace).as_posix()
    try:
        r = ctx.sandbox.execute(f"python {rel}", timeout=timeout)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    return out.strip() if out.strip() else "(no output)"


def _get_osb_interp(ctx: ToolContext):
    """Return an OpenSandboxInterpreter when env+sandbox allow it, else None.

    Gated on:
    - ``MINI_CC_REPL_BACKEND=opensandbox`` (explicit opt-in)
    - ``ctx.sandbox`` has a ``_mgr`` (i.e. is ContainerSandbox)
    - That manager's runtime is OpenSandboxRuntime
    - ``runtime.interp_for(tid)`` succeeds (sandbox exists)

    Any failure (no env, no container, sandbox not yet started) returns
    None and the caller falls back to the local pickle-wrapper path."""
    import os
    if os.environ.get("MINI_CC_REPL_BACKEND") != "opensandbox":
        return None
    mgr = getattr(ctx.sandbox, "_mgr", None)
    if mgr is None:
        return None
    rt = getattr(mgr, "runtime", None)
    if rt is None or type(rt).__name__ != "OpenSandboxRuntime":
        return None
    try:
        return rt.interp_for(mgr.tid)
    except Exception:
        return None


def _run_javascript(ctx: ToolContext, code: str, *, timeout: int) -> str:
    if shutil.which("node") is None:
        return ("Error: node is not installed on the sandbox host; "
                "cannot run JavaScript. Use python or shell instead.")
    # Write code to a temp file so we avoid quoting issues.
    workspace = Path(ctx.sandbox.project_root)  # type: ignore[attr-defined]
    script_path = (workspace / ".mini_cc" / "repl"
                   / f"{ctx.session_id}.js.run.js")
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(code, encoding="utf-8")
    rel = script_path.relative_to(workspace).as_posix()
    try:
        r = ctx.sandbox.execute(f"node {rel}", timeout=timeout)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    return out.strip() if out.strip() else "(no output)"


def _run_shell(ctx: ToolContext, code: str, *, timeout: int) -> str:
    try:
        r = ctx.sandbox.execute(code, timeout=timeout)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    return out.strip() if out.strip() else "(no output)"


def _exec_code(ctx: ToolContext, args: dict) -> str:
    lang = (args.get("language") or "").strip().lower()
    code = args.get("code") or ""
    reset = bool(args.get("reset", False))
    timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
    if not code:
        return "Error: code is required."
    if lang not in SUPPORTED_LANGUAGES:
        return (f"Error: unsupported language {lang!r}. "
                f"Choose one of {', '.join(SUPPORTED_LANGUAGES)}.")
    if lang == "python":
        return _run_python(ctx, code, reset=reset, timeout=timeout)
    if lang == "javascript":
        return _run_javascript(ctx, code, timeout=timeout)
    return _run_shell(ctx, code, timeout=timeout)


EXEC_CODE_TOOL = FunctionTool(
    name="execute_code",
    description=(
        "Execute code in the project sandbox. Stateful for python: "
        "variables, imports, and mutated objects persist across calls "
        "within the same session (state is pickled to "
        "<workspace>/.mini_cc/repl/<session>.python.pickle). Use "
        "`reset: true` to wipe prior state and start fresh. JavaScript "
        "(Node) and shell are stateless one-shot invocations. All "
        "execution honors the sandbox's policy and cwd — works the same "
        "whether the project uses SubprocessSandbox or a Docker container."),
    input_schema={
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": list(SUPPORTED_LANGUAGES),
                "description": "python | javascript | shell.",
            },
            "code": {
                "type": "string",
                "description": "Source to execute. For python, multi-line "
                               "scripts are fine (imports, classes, etc.).",
            },
            "reset": {
                "type": "boolean",
                "description": "Python only: drop prior state before "
                               "running. Default false.",
            },
            "timeout": {
                "type": "integer",
                "description": "Per-call timeout in seconds (default 60).",
            },
        },
        "required": ["language", "code"],
    },
    fn=_exec_code,
)


ALL = [EXEC_CODE_TOOL]
