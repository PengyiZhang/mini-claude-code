"""OpenSandboxInterpreter: stateful code execution via execd /code endpoints.

Two-step protocol (spec: specs/execd-api.yaml):
1. POST /code/context {language} → {id, language}. Lazy, cached per
   language so subsequent calls reuse the same Jupyter kernel session.
2. POST /code {context:{id,language}, code:"..."} → text/event-stream
   of ServerStreamEvent JSON-per-line (same framing as /command).

Events we consume: stdout/stderr (text), result (results dict keyed by
MIME type — we pick text/plain), error (structured traceback), and
execution_complete (terminal)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO

import pytest


# ── helpers ────────────────────────────────────────────────────────────────

class _MockResp(BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _mock_urlopen(monkeypatch, responder):
    """responder: callable(Request) -> (status, body_bytes)."""
    def fake(req, *a, **kw):
        status, body = responder(req)
        r = _MockResp(body if isinstance(body, bytes) else body.encode())
        r.status = status
        return r
    monkeypatch.setattr("mini_cc.tools.opensandbox_interp.urlopen", fake)


def _sse_lines(*objs: dict) -> bytes:
    """Encode each dict as one JSON line — the real execd stream format."""
    return "\n".join(json.dumps(o) for o in objs).encode() + b"\n"


# ── context creation ───────────────────────────────────────────────────────

def test_run_creates_context_lazily_then_reuses(monkeypatch):
    """First call POSTs /code/context; second call with same language
    reuses the cached context_id and skips the create."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    posted = []
    def responder(req):
        posted.append(req.full_url)
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "ctx_1", "language": "python"}))
        return (200, _sse_lines(
            {"type": "execution_complete", "execution_time": 1}))
    _mock_urlopen(monkeypatch, responder)
    interp = OpenSandboxInterpreter(
        sandbox_id="sbx", execd_url="http://x", execd_headers={})
    interp.run("x = 1", language="python")
    interp.run("print(x)", language="python")
    creates = [u for u in posted if u.endswith("/code/context")]
    assert len(creates) == 1  # cached after first


def test_context_creation_separate_per_language(monkeypatch):
    """Python and bash get distinct contexts (separate Jupyter kernels)."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    posted = []
    def responder(req):
        posted.append((req.full_url, json.loads(req.data.decode())))
        if req.full_url.endswith("/code/context"):
            body = json.loads(req.data.decode())
            return (200, json.dumps({"id": f"ctx_{body['language']}",
                                     "language": body["language"]}))
        return (200, _sse_lines({"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    interp = OpenSandboxInterpreter("sbx", "http://x", {})
    interp.run("x=1", language="python")
    interp.run("echo hi", language="bash")
    creates = [(u, b) for u, b in posted if u.endswith("/code/context")]
    langs = sorted(b["language"] for _, b in creates)
    assert langs == ["bash", "python"]


# ── stream parsing ─────────────────────────────────────────────────────────

def test_run_returns_stdout_and_result_text(monkeypatch):
    """result event's results dict is MIME-keyed; we pick text/plain."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "c1", "language": "python"}))
        return (200, _sse_lines(
            {"type": "stdout", "text": "computing..."},
            {"type": "result", "results": {"text/plain": "4"}},
            {"type": "execution_complete", "execution_time": 12}))
    _mock_urlopen(monkeypatch, responder)
    interp = OpenSandboxInterpreter("sbx", "http://x", {})
    out = interp.run("2 + 2", language="python")
    assert "4" in out          # result text
    assert "computing..." in out  # stdout


def test_run_returns_stderr(monkeypatch):
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "c1", "language": "python"}))
        return (200, _sse_lines(
            {"type": "stderr", "text": "warning!"},
            {"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    out = interp = OpenSandboxInterpreter("sbx", "http://x", {}).run(
        "import warnings", language="python")
    assert "warning!" in out


def test_run_formats_structured_error(monkeypatch):
    """error events carry ename/evalue/traceback — surface them so the
    agent can see why their code blew up, not just a blank response."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "c1", "language": "python"}))
        return (200, _sse_lines(
            {"type": "error", "error": {
                "ename": "ZeroDivisionError",
                "evalue": "division by zero",
                "traceback": ["line 1: 1/0"]}},
            {"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    out = OpenSandboxInterpreter("sbx", "http://x", {}).run(
        "1/0", language="python")
    assert "ZeroDivisionError" in out
    assert "division by zero" in out


def test_run_returns_no_output_message_when_empty(monkeypatch):
    """A successful run with no stdout/result shouldn't return an empty
    string — the agent may misread that as failure."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "c1", "language": "python"}))
        return (200, _sse_lines({"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    out = OpenSandboxInterpreter("sbx", "http://x", {}).run(
        "x = 1", language="python")
    assert out  # non-empty


# ── request shape ─────────────────────────────────────────────────────────

def test_code_request_includes_context_id_and_code(monkeypatch):
    """POST /code body matches spec: {context: {id, language}, code}."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    captured = {}
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "ctx_xyz", "language": "python"}))
        captured["body"] = json.loads(req.data.decode())
        captured["url"] = req.full_url
        return (200, _sse_lines({"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxInterpreter("sbx", "http://x", {}).run(
        "print('hi')", language="python")
    assert captured["body"]["context"] == {"id": "ctx_xyz",
                                           "language": "python"}
    assert captured["body"]["code"] == "print('hi')"
    assert captured["url"] == "http://x/code"


def test_execd_headers_forwarded(monkeypatch):
    """Auth headers from the execd endpoint discovery flow reach /code."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    seen = {}
    def responder(req):
        if req.full_url.endswith("/code/context"):
            return (200, json.dumps({"id": "c1", "language": "python"}))
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return (200, _sse_lines({"type": "execution_complete"}))
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxInterpreter(
        "sbx", "http://x",
        {"X-Sandbox-Token": "tok-abc"}).run("1", language="python")
    assert seen["headers"].get("x-sandbox-token") == "tok-abc"


# ── connection error ──────────────────────────────────────────────────────

def test_run_surfaces_connection_error(monkeypatch):
    """Network failure shouldn't crash the agent — return an Error: line."""
    from mini_cc.tools.opensandbox_interp import OpenSandboxInterpreter
    def boom(req, *a, **kw):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr("mini_cc.tools.opensandbox_interp.urlopen", boom)
    out = OpenSandboxInterpreter("sbx", "http://x", {}).run(
        "1", language="python")
    assert out.startswith("Error:")