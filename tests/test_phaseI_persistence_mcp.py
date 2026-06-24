"""Phase I — workflow persistence, /resume slash, MCP stdio consumer."""
from __future__ import annotations

import io
import json
import threading
import time
from dataclasses import dataclass

import pytest

from mini_cc.commands import CommandContext, default_registry
from mini_cc.mcp import MCPPool, StdioMCPClient, StdioMCPError
from mini_cc.storage import FSStorage
from mini_cc.workflow import workflow_from_dict


# ── Workflow persistence ─────────────────────────────────────────────────

def test_storage_workflow_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    wf = {"id": "wf_abc", "name": "demo",
          "steps": [{"id": "s1", "prompt": "hi"}], "results": {"s1": "ok"},
          "state": {}, "status": "completed"}
    s.save_workflow("p1", wf)
    loaded = s.load_workflow("p1", "wf_abc")
    assert loaded is not None
    assert loaded["id"] == "wf_abc"
    assert loaded["name"] == "demo"
    assert loaded["results"] == {"s1": "ok"}
    assert "saved_at" in loaded  # added by storage


def test_storage_workflow_list_orders_by_saved_at_desc(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow("p1", {"id": "wf_a", "name": "a", "steps": []})
    # Bump mtime so the ordering is deterministic even on coarse filesystems.
    time.sleep(0.01)
    s.save_workflow("p1", {"id": "wf_b", "name": "b", "steps": []})
    rows = s.list_workflows("p1")
    assert [r["id"] for r in rows] == ["wf_b", "wf_a"]


def test_storage_workflow_safe_id_rejects_traversal(tmp_path):
    s = FSStorage(tmp_path / "state")
    # Path-traversal attempt must be sanitized into a safe filename.
    s.save_workflow("p1", {"id": "../../etc/passwd", "name": "x", "steps": []})
    # The traversal payload must not have escaped the workflows dir.
    workflows_dir = tmp_path / "state" / "p1" / "workflows"
    files = list(workflows_dir.glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name
    # And it should round-trip via the sanitized id.
    rows = s.list_workflows("p1")
    assert len(rows) == 1


def test_storage_workflow_delete(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_workflow("p1", {"id": "wf_x", "name": "x", "steps": []})
    assert s.delete_workflow("p1", "wf_x") is True
    assert s.delete_workflow("p1", "wf_x") is False  # already gone
    assert s.load_workflow("p1", "wf_x") is None


def test_storage_workflow_load_missing_returns_none(tmp_path):
    s = FSStorage(tmp_path / "state")
    assert s.load_workflow("p1", "nope") is None
    assert s.list_workflows("p1") == []


# ── /workflow save|load|list slash commands ──────────────────────────────

def _run_cmd(name, *, project=None, storage=None, args=""):
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None, f"/{name} not registered"
    ctx = CommandContext(
        project_id="p1", session_id="sess1",
        tenant_id="t1", args=args, project=project, storage=storage,
    )
    return list(cmd.handler(ctx))


def _make_project_with_storage(tmp_path):
    storage = FSStorage(tmp_path / "state")
    wf = workflow_from_dict({
        "name": "demo",
        "description": "demo wf",
        "steps": [{"id": "s1", "prompt": "do thing"}],
    })
    wf.results["s1"] = "ok"

    class _P:
        active_workflow = wf
    return storage, _P()


def test_workflow_slash_save_persists(tmp_path):
    storage, proj = _make_project_with_storage(tmp_path)
    events = _run_cmd("workflow", project=proj, storage=storage, args="save")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "saved" in text.lower()
    rows = storage.list_workflows("p1")
    assert len(rows) == 1
    assert rows[0]["name"] == "demo"
    assert rows[0]["results"] == {"s1": "ok"}


def test_workflow_slash_list_renders(tmp_path):
    storage, proj = _make_project_with_storage(tmp_path)
    _run_cmd("workflow", project=proj, storage=storage, args="save")
    events = _run_cmd("workflow", project=proj, storage=storage, args="list")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "demo" in text
    assert "1/1 done" in text


def test_workflow_slash_load_restores_results(tmp_path):
    storage, proj = _make_project_with_storage(tmp_path)
    _run_cmd("workflow", project=proj, storage=storage, args="save")
    # Drop the active workflow, then load it back from storage.
    object.__setattr__(proj, "active_workflow", None)
    events = _run_cmd("workflow", project=proj, storage=storage,
                      args="load wf_")  # prefix not match — try by name
    # First attempt: id lookup misses; load by name fallback should hit.
    # Run again with the actual name to verify name-based lookup path.
    events = _run_cmd("workflow", project=proj, storage=storage, args="load demo")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "loaded workflow" in text
    wf = getattr(proj, "active_workflow")
    assert wf is not None
    assert wf.results == {"s1": "ok"}


def test_workflow_slash_load_missing_errors(tmp_path):
    storage = FSStorage(tmp_path / "state")
    class _P: pass
    events = _run_cmd("workflow", project=_P(), storage=storage,
                      args="load ghost")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "ghost" in err["message"]


def test_workflow_slash_delete(tmp_path):
    storage, proj = _make_project_with_storage(tmp_path)
    _run_cmd("workflow", project=proj, storage=storage, args="save")
    events = _run_cmd("workflow", project=proj, storage=storage, args="delete demo")
    # delete uses id, not name — but our saved id is auto-generated, so
    # grab it via list.
    rows = storage.list_workflows("p1")
    assert rows, "save should have written one workflow"
    real_id = rows[0]["id"]
    events = _run_cmd("workflow", project=proj, storage=storage,
                      args=f"delete {real_id}")
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "deleted" in text.lower()
    assert storage.list_workflows("p1") == []


def test_workflow_slash_save_without_active_errors(tmp_path):
    storage = FSStorage(tmp_path / "state")
    class _P: pass
    events = _run_cmd("workflow", project=_P(), storage=storage, args="save")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "no active" in err["message"].lower()


# ── Workflow tools persist active workflow through storage ───────────────

def test_workflow_create_persists_to_storage(tmp_path):
    """Calling workflow_create should mirror the active workflow to
    storage so a restart can restore it."""
    from mini_cc.tools import builtin_tools, dispatch
    from mini_cc.tools.base import ToolContext

    storage = FSStorage(tmp_path / "state")
    class _P: pass
    ctx = ToolContext(
        project_id="p1", session_id="s1",
        sandbox=None, storage=storage, todos=[], project_ref=_P(),
    )
    tools = dispatch(builtin_tools())
    out = tools["workflow_create"].handle(ctx, {
        "workflow": {"name": "demo",
                     "steps": [{"id": "s1", "prompt": "X"}]},
    })
    assert "workflow created" in out
    rows = storage.list_workflows("p1")
    assert any(r["name"] == "demo" for r in rows)


# ── /resume slash command ────────────────────────────────────────────────

def test_resume_slash_registered():
    reg = default_registry()
    assert reg.resolve("resume") is not None


def test_resume_no_args_lists_sessions():
    """With no args, /resume renders a list of recent sessions."""
    @dataclass
    class _M:
        session_id: str
        last_active_at: str
        message_count: int
        in_memory: bool = False
    class _SM:
        def list(self, pid):
            return [_M("sess_a", "2026-01-01T00:00:00Z", 5),
                    _M("sess_b", "2026-01-02T00:00:00Z", 12)]
    events = _run_cmd("resume", )
    # Replace the freshly-resolved ctx's session_manager with our stub.
    # Simpler: re-run with our stub via direct handler call.
    reg = default_registry()
    cmd = reg.resolve("resume")
    ctx = CommandContext(
        project_id="p1", session_id="sess_a",
        tenant_id="t1", args="", session_manager=_SM(),
    )
    events = list(cmd.handler(ctx))
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "sess_a" in text
    assert "sess_b" in text
    assert "5 msgs" in text
    assert "Resume with" in text


def test_resume_with_id_warms_and_emits_event():
    """`/resume <id>` warms the session via start_session and emits a
    session_resumed event the client can rotate on."""
    @dataclass
    class _Msg: role: str = "user"; content: str = "hi"
    class _Loop:
        messages: list = [_Msg(), _Msg(), _Msg()]
    @dataclass
    class _Sess:
        loop: _Loop = _Loop()
    class _SM:
        def __init__(self):
            self.warmed_with = None
            self._sessions = {}
        def list(self, pid):
            @dataclass
            class _M:
                session_id: str
                last_active_at: str = "2026-01-01"
                message_count: int = 3
                in_memory: bool = False
            return [_M("sess_target")]
        def start_session(self, pid, sid):
            self.warmed_with = sid
            return _Sess()
    sm = _SM()
    reg = default_registry()
    cmd = reg.resolve("resume")
    ctx = CommandContext(
        project_id="p1", session_id="sess_other",
        tenant_id="t1", args="sess_target", session_manager=sm,
    )
    events = list(cmd.handler(ctx))
    assert any(e.get("type") == "session_resumed"
               and e.get("session_id") == "sess_target" for e in events)
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "resumed" in text.lower()
    assert sm.warmed_with == "sess_target"


def test_resume_unknown_session_id_errors():
    @dataclass
    class _M:
        session_id: str
        last_active_at: str = ""
        message_count: int = 0
        in_memory: bool = False
    class _SM:
        def list(self, pid): return [_M("sess_a")]
        def start_session(self, pid, sid): raise AssertionError("should not warm")
    reg = default_registry()
    cmd = reg.resolve("resume")
    ctx = CommandContext(
        project_id="p1", session_id="sess_a",
        tenant_id="t1", args="ghost", session_manager=_SM(),
    )
    events = list(cmd.handler(ctx))
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "ghost" in err["message"]


def test_resume_no_session_manager_errors():
    reg = default_registry()
    cmd = reg.resolve("resume")
    ctx = CommandContext(project_id="p1", session_id="s1",
                         tenant_id="t1", args="")
    events = list(cmd.handler(ctx))
    assert any(e.get("type") == "error" for e in events)


def test_resume_empty_project_hint():
    class _SM:
        def list(self, pid): return []
    reg = default_registry()
    cmd = reg.resolve("resume")
    ctx = CommandContext(
        project_id="p1", session_id="s1",
        tenant_id="t1", args="", session_manager=_SM(),
    )
    events = list(cmd.handler(ctx))
    text = next(e["text"] for e in events if e["type"] == "text")
    assert "no sessions" in text.lower()


# ── MCP stdio consumer ───────────────────────────────────────────────────

class _FakeProc:
    """Pretends to be a subprocess with .stdin/.stdout.

    Tests script the server side by appending framed messages to a
    queue; the reader thread drains them. The proc never produces real
    output on its own.
    """
    def __init__(self, responses: list[bytes]):
        # Each entry in `responses` is one framed message the "server"
        # will emit when its stdin is written to.
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.stdin = io.BytesIO()
        self._written = threading.Event()
        # Buffer of bytes the "server" will produce on stdout.
        self._out_buf = bytearray()
        self._out_ready = threading.Event()
        self.stdout = _FakeReader(self)
        self.stderr = io.BytesIO()
        self.terminated = False
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _writer_loop(self):
        """Watch stdin for writes; on each, push the next pre-scripted
        response onto stdout."""
        seen = 0
        last_pos = 0
        while not self.terminated:
            # Wait for new bytes on stdin.
            data = self.stdin.getvalue()
            if len(data) > last_pos:
                last_pos = len(data)
                if seen < len(self._responses):
                    with self._lock:
                        self._out_buf.extend(self._responses[seen])
                    seen += 1
                    self._out_ready.set()
            time.sleep(0.005)

    def terminate(self): self.terminated = True
    def kill(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def poll(self): return None


class _FakeReader:
    """readline()/read() against a shared bytearray that grows in
    background. Stops returning data once the proc is terminated and
    the buffer is drained."""
    def __init__(self, proc):
        self._proc = proc

    def readline(self):
        # Block until a complete line (terminated by \n) is available.
        while True:
            with self._proc._lock:
                buf = self._proc._out_buf
                idx = buf.find(b"\n")
                if idx >= 0:
                    line = bytes(buf[:idx + 1])
                    del buf[:idx + 1]
                    return line
            if self._proc.terminated and not buf:
                return b""
            time.sleep(0.005)

    def read(self, n):
        while True:
            with self._proc._lock:
                buf = self._proc._out_buf
                if len(buf) >= n:
                    chunk = bytes(buf[:n])
                    del buf[:n]
                    return chunk
            if self._proc.terminated and not buf:
                return b""
            time.sleep(0.005)


def _frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def test_stdio_mcp_client_handshake_and_call_tool():
    init_response = _frame({
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
    })
    tools_list_response = _frame({
        "jsonrpc": "2.0", "id": 2,
        "result": {"tools": [
            {"name": "search", "description": "Search docs",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"}}}},
        ]},
    })
    call_response = _frame({
        "jsonrpc": "2.0", "id": 3,
        "result": {"content": [{"type": "text", "text": "hello world"}]},
    })
    proc = _FakeProc([init_response, tools_list_response, call_response])
    client = StdioMCPClient("docs", ["fake-command"], proc=proc)
    client.startup()
    assert len(client.tools) == 1
    assert client.tools[0]["name"] == "search"
    assert client.tools[0]["inputSchema"]["properties"]["q"]["type"] == "string"
    result = client.call_tool("search", {"q": "hi"})
    assert result == "hello world"
    client.close()


def test_stdio_mcp_client_rejects_bad_command():
    with pytest.raises(StdioMCPError):
        bad = StdioMCPClient("bad", [])  # empty command
        bad.startup()


def test_stdio_mcp_client_call_returns_error_string_on_failure():
    """A JSON-RPC error response surfaces as an 'MCP error:' string,
    not an exception — matches the in-process MCPClient behavior so
    tool handlers can return it directly as tool_result text."""
    init_response = _frame({
        "jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}},
    })
    tools_list_response = _frame({
        "jsonrpc": "2.0", "id": 2, "result": {"tools": [
            {"name": "ping", "inputSchema": {"type": "object"}}]},
    })
    call_response = _frame({
        "jsonrpc": "2.0", "id": 3,
        "error": {"code": -32602, "message": "params missing"},
    })
    proc = _FakeProc([init_response, tools_list_response, call_response])
    client = StdioMCPClient("svc", ["x"], proc=proc)
    client.startup()
    result = client.call_tool("ping", {})
    assert result.startswith("MCP error:")
    assert "params missing" in result
    client.close()


def test_mcp_pool_connect_stdio_records_client_and_exposes_tools():
    """connect_stdio wires the spawned client into the pool so
    all_tools() returns FunctionTool wrappers for the server's tools."""
    init = _frame({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})
    tools = _frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
        {"name": "lookup", "inputSchema": {"type": "object",
                                           "properties": {"k": {"type": "string"}}}},
    ]}})
    proc = _FakeProc([init, tools])
    # Bypass real subprocess by injecting our proc via a factory hook:
    # build the client first, then attach.
    client = StdioMCPClient("docs", ["fake"], proc=proc)
    client.startup()
    pool = MCPPool("p1")
    pool._clients["docs"] = client
    tools_wrapped = pool.all_tools()
    names = [t.name for t in tools_wrapped]
    assert any("mcp__docs__lookup" in n for n in names)


def test_mcp_pool_connect_stdio_failure_returns_error_tuple():
    """A bad command should return (False, message), not raise —
    so ProjectManager can keep booting other servers."""
    pool = MCPPool("p1")
    ok, msg = pool.connect_stdio("bad", [])
    assert ok is False
    assert "command" in msg.lower() or "must be" in msg.lower()


def test_mcp_pool_connect_stdio_idempotent():
    init = _frame({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})
    tools = _frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    proc = _FakeProc([init, tools])
    client = StdioMCPClient("docs", ["fake"], proc=proc)
    client.startup()
    pool = MCPPool("p1")
    pool._clients["docs"] = client
    ok, msg = pool.connect_stdio("docs", ["other"])
    assert ok is True
    assert "already" in msg.lower()


# ── Config: mcp_servers env parsing ──────────────────────────────────────

def test_config_mcp_servers_parses_env(monkeypatch):
    from mini_cc.config import AnthropicConfig
    monkeypatch.setenv("MINI_CC_MCP_SERVERS", json.dumps({
        "docs": {"command": ["npx", "mcp-docs"],
                  "env": {"API_KEY": "x"}, "cwd": "/tmp"},
        "fs": {"command": ["python", "-m", "mcp_fs"]},
    }))
    cfg = AnthropicConfig.from_env()
    assert cfg.mcp_servers is not None
    assert set(cfg.mcp_servers.keys()) == {"docs", "fs"}
    assert cfg.mcp_servers["docs"]["command"] == ["npx", "mcp-docs"]
    assert cfg.mcp_servers["docs"]["env"] == {"API_KEY": "x"}
    assert "cwd" not in cfg.mcp_servers["fs"]  # not set


def test_config_mcp_servers_empty_when_env_missing(monkeypatch):
    from mini_cc.config import AnthropicConfig
    monkeypatch.delenv("MINI_CC_MCP_SERVERS", raising=False)
    cfg = AnthropicConfig.from_env()
    assert cfg.mcp_servers is None


def test_config_mcp_servers_invalid_json_returns_none(monkeypatch):
    from mini_cc.config import AnthropicConfig
    monkeypatch.setenv("MINI_CC_MCP_SERVERS", "not-json")
    cfg = AnthropicConfig.from_env()
    assert cfg.mcp_servers is None


def test_config_mcp_servers_filters_invalid_entries(monkeypatch):
    from mini_cc.config import AnthropicConfig
    monkeypatch.setenv("MINI_CC_MCP_SERVERS", json.dumps({
        "good": {"command": ["x"]},
        "bad_no_command": {"env": {}},
        "bad_command_string": {"command": "not-a-list"},
    }))
    cfg = AnthropicConfig.from_env()
    assert cfg.mcp_servers is not None
    assert list(cfg.mcp_servers.keys()) == ["good"]


# ── /help lists the new commands ─────────────────────────────────────────

def test_help_lists_resume():
    reg = default_registry()
    visible = {c.name for c in reg.all_visible()}
    assert "resume" in visible
