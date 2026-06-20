"""MCPPool + connect_mcp + dynamic tool merging tests."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from mini_cc import MCPClient, MCPPool
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _clear_factories():
    """Isolate tests from each other's factory registrations."""
    MCPPool.reset_factories()
    yield
    MCPPool.reset_factories()


def _docs_factory():
    return MCPClient(
        "docs",
        tool_defs=[
            {"name": "search", "description": "Search docs.",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version.",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )


# ── MCPPool basics ───────────────────────────────────────────────────────

def test_connect_unknown_server_fails():
    pool = MCPPool("p")
    ok, msg = pool.connect("nonexistent")
    assert ok is False
    assert "Unknown server" in msg


def test_register_factory_then_connect():
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    ok, msg = pool.connect("docs")
    assert ok is True
    assert "Discovered 2 tools" in msg


def test_double_connect_is_idempotent():
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    pool.connect("docs")
    ok, msg = pool.connect("docs")
    assert ok is True
    assert "already connected" in msg


def test_disconnect():
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    pool.connect("docs")
    assert pool.disconnect("docs") is True
    assert pool.list_connected() == []
    assert pool.disconnect("docs") is False


def test_list_connected_sorted():
    MCPPool.register_factory("docs", _docs_factory)
    MCPPool.register_factory("deploy", lambda: MCPClient(
        "deploy",
        tool_defs=[{"name": "status", "description": "x",
                    "inputSchema": {"type": "object", "properties": {}}}],
        handlers={"status": lambda: "ok"}))
    pool = MCPPool("p")
    pool.connect("deploy")
    pool.connect("docs")
    assert pool.list_connected() == ["deploy", "docs"]


def test_pool_per_project_isolation():
    """Two pools on the same factory registry should hold independent state."""
    MCPPool.register_factory("docs", _docs_factory)
    a = MCPPool("proj-a")
    b = MCPPool("proj-b")
    a.connect("docs")
    assert a.list_connected() == ["docs"]
    assert b.list_connected() == []  # b did not connect


# ── Tool wrappers ────────────────────────────────────────────────────────

def test_all_tools_emits_prefixed_wrappers():
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    pool.connect("docs")
    tools = pool.all_tools()
    names = sorted(t.name for t in tools)
    assert names == ["mcp__docs__get_version", "mcp__docs__search"]


def test_wrapper_dispatches_to_handler():
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    pool.connect("docs")
    tools = {t.name: t for t in pool.all_tools()}
    ctx = ToolContext(project_id="p", session_id="s",
                      sandbox=None, storage=None, todos=[], mcp_pool=pool)
    out = tools["mcp__docs__search"].handle(ctx, {"query": "python"})
    assert "[docs]" in out
    assert "python" in out


def test_normalize_mcp_name():
    from mini_cc import normalize_mcp_name
    assert normalize_mcp_name("foo-bar.baz") == "foo-bar_baz"
    assert normalize_mcp_name("clean_name") == "clean_name"


# ── connect_mcp tool ─────────────────────────────────────────────────────

def _ctx(pool, tmp_path):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    return ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                       storage=storage, todos=[], mcp_pool=pool)


def test_connect_mcp_tool(tmp_path):
    MCPPool.register_factory("docs", _docs_factory)
    pool = MCPPool("p")
    ctx = _ctx(pool, tmp_path)
    tools = dispatch(builtin_tools())
    out = tools["connect_mcp"].handle(ctx, {"name": "docs"})
    assert "Connected" in out
    assert pool.list_connected() == ["docs"]


def test_connect_mcp_tool_unknown(tmp_path):
    pool = MCPPool("p")
    ctx = _ctx(pool, tmp_path)
    tools = dispatch(builtin_tools())
    out = tools["connect_mcp"].handle(ctx, {"name": "nope"})
    assert "Error" in out


def test_connect_mcp_tool_without_pool(tmp_path):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], mcp_pool=None)
    tools = dispatch(builtin_tools())
    out = tools["connect_mcp"].handle(ctx, {"name": "x"})
    assert "not configured" in out.lower()


# ── AgentLoop integration: MCP tools appear mid-session ──────────────────

@dataclass
class _Block:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict | None = None
    id: str | None = None


class _MockResponse:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _MockClient:
    def __init__(self, script):
        self.script = list(script)
        outer = self

        class _Stream:
            def __init__(self_inner, response):
                self_inner._response = response
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
            def __iter__(self_inner):
                return iter(())
            def get_final_message(self_inner):
                return self_inner._response
            def close(self_inner):
                pass

        class _M:
            def create(self_i, **kw):
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return outer.script.pop(0)

            def stream(self_i, **kw):
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return _Stream(outer.script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


def test_loop_picks_up_mcp_tool_after_connect(tmp_path):
    """A turn that calls connect_mcp mid-session should make the MCP tool
    callable on the very next iteration."""
    MCPPool.register_factory("docs", _docs_factory)
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    pool = MCPPool("p")
    ref = ProjectRef(
        project_id="p", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage, mcp_pool=pool,
        client_factory=lambda: _MockClient([
            # First: agent calls connect_mcp
            _MockResponse([_Block(type="tool_use", name="connect_mcp",
                                  id="tu1", input={"name": "docs"})]),
            # Second: agent invokes the newly-available MCP tool
            _MockResponse([_Block(type="tool_use", name="mcp__docs__search",
                                  id="tu2", input={"query": "x"})]),
            # Third: agent is done
            _MockResponse([_Block(type="text", text="ok")], stop_reason="end_turn"),
        ]),
    )
    loop = AgentLoop(ref, "sess1")
    events = list(loop.run("connect then search"))
    tool_uses = [e for e in events if e["type"] == "tool_use"]
    tool_names = [e["name"] for e in tool_uses]
    assert "connect_mcp" in tool_names
    assert "mcp__docs__search" in tool_names  # MCP tool was callable

    # The MCP tool produced a real handler-backed result
    tr = next(e for e in events
              if e["type"] == "tool_result"
              and e.get("content", "").startswith("[docs]"))
    assert "x" in tr["content"]
