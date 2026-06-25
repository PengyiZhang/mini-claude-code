"""Tests for Claude Code .mcp.json compatibility + multi-transport MCP.

Covers:
- ``.mcp.json`` parsing (Claude Code format) at any tier
- Type inference: command⇒stdio, url⇒http
- Legacy ``mcp.toml`` still works alongside
- ``.mcp.json`` overrides ``mcp.toml`` within the same tier on name clash
- ``MCPPool.connect_from_spec`` dispatches by ``type``
- ``HttpMCPClient`` against an in-process fake server (urllib-sized)
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from mini_cc.plugins import (PluginTier, discover_mcp_servers,
                              ensure_tier_dir, write_mcp_servers)
from mini_cc.mcp import HttpMCPClient, HttpMCPError, MCPPool, SseMCPClient


# ── .mcp.json parsing ────────────────────────────────────────────────

def _write_json(tier_dir: Path, servers: dict[str, dict]) -> None:
    fp = tier_dir / ".mcp.json"
    fp.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_mcp_json_stdio_with_command_string(tmp_path):
    """Claude Code shape: ``command`` is a string + ``args`` is a list."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_json(sys_dir, {
        "docs": {"command": "npx", "args": ["mcp-server-docs"]}
    })
    out = discover_mcp_servers([sys_dir])
    assert out["docs"]["type"] == "stdio"
    assert out["docs"]["command"] == ["npx", "mcp-server-docs"]


def test_mcp_json_http_type_inferred_from_url(tmp_path):
    """``url`` without explicit ``type`` ⇒ http."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_json(sys_dir, {
        "remote": {"url": "https://example.com/mcp",
                   "headers": {"Authorization": "Bearer xyz"}}
    })
    out = discover_mcp_servers([sys_dir])
    assert out["remote"]["type"] == "http"
    assert out["remote"]["url"] == "https://example.com/mcp"
    assert out["remote"]["headers"]["Authorization"] == "Bearer xyz"


def test_mcp_json_explicit_sse_type(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_json(sys_dir, {
        "old": {"type": "sse", "url": "https://example.com/sse"}
    })
    out = discover_mcp_servers([sys_dir])
    assert out["old"]["type"] == "sse"


def test_mcp_json_overrides_toml_in_same_tier(tmp_path):
    """When both files exist in the same tier, .mcp.json wins on clash."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    write_mcp_servers(sys_dir, {"docs": {"command": ["from-toml"]}})
    _write_json(sys_dir, {"docs": {"command": "from-json",
                                   "args": ["--port", "8080"]}})
    out = discover_mcp_servers([sys_dir])
    assert out["docs"]["command"] == ["from-json", "--port", "8080"]


def test_mcp_json_and_toml_merge_distinct_keys(tmp_path):
    """Distinct keys from both files all show up."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    write_mcp_servers(sys_dir, {"from-toml": {"command": ["a"]}})
    _write_json(sys_dir, {"from-json": {"command": "b"}})
    out = discover_mcp_servers([sys_dir])
    assert set(out) == {"from-toml", "from-json"}


def test_mcp_json_propagates_across_tiers(tmp_path):
    """JSON format works at any tier; project > tenant > system."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    ten_dir = ensure_tier_dir(tmp_path / "t", PluginTier.TENANT,
                              tenant_id="t")
    _write_json(sys_dir, {"shared": {"command": "sys"}})
    _write_json(ten_dir, {"shared": {"command": "ten"}})
    out = discover_mcp_servers([sys_dir, ten_dir])
    assert out["shared"]["command"] == ["ten"]


def test_mcp_json_unknown_type_skipped(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_json(sys_dir, {"weird": {"type": "carrier-pigeon"}})
    out = discover_mcp_servers([sys_dir])
    assert "weird" not in out


def test_mcp_json_malformed_skipped(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    (sys_dir / ".mcp.json").write_text("{not json", encoding="utf-8")
    out = discover_mcp_servers([sys_dir])
    assert out == {}


# ── MCPPool.connect_from_spec dispatch ────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_factories():
    MCPPool.reset_factories()
    yield
    MCPPool.reset_factories()


def test_connect_from_spec_unknown_type_returns_error():
    pool = MCPPool("p")
    ok, msg = pool.connect_from_spec("x", {"type": "unknown"})
    assert ok is False
    assert "unknown type" in msg


def test_connect_from_spec_stdio_routes_to_connect_stdio(monkeypatch):
    pool = MCPPool("p")
    captured: dict[str, Any] = {}
    def fake_stdio(self, name, command, *, env=None, cwd=None):
        captured.update(name=name, command=command, env=env, cwd=cwd)
        return True, "ok"
    monkeypatch.setattr(MCPPool, "connect_stdio", fake_stdio)
    pool.connect_from_spec("docs", {
        "type": "stdio", "command": ["npx", "x"], "env": {"K": "v"}
    })
    assert captured["command"] == ["npx", "x"]
    assert captured["env"] == {"K": "v"}


def test_connect_from_spec_http_routes_to_connect_http(monkeypatch):
    pool = MCPPool("p")
    captured: dict[str, Any] = {}
    def fake_http(self, name, url, *, headers=None):
        captured.update(name=name, url=url, headers=headers)
        return True, "ok"
    monkeypatch.setattr(MCPPool, "connect_http", fake_http)
    pool.connect_from_spec("r", {
        "type": "http", "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer t"},
    })
    assert captured["url"] == "https://x/mcp"
    assert captured["headers"] == {"Authorization": "Bearer t"}


def test_connect_from_spec_stdio_missing_command_fails():
    pool = MCPPool("p")
    ok, msg = pool.connect_from_spec("x", {"type": "stdio"})
    assert ok is False
    assert "missing command" in msg


# ── HttpMCPClient against a fake Streamable HTTP server ───────────────

class _FakeStreamableHandler(BaseHTTPRequestHandler):
    """Minimal MCP-over-HTTP stand-in.

    Recognises the initialize / tools/list / tools/call JSON-RPC methods
    and returns canned responses. Designed to exercise the production
    ``HttpMCPClient`` end-to-end without spinning up a real MCP server.
    """

    server_version = "FakeMCP/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs):
        pass  # silence stderr noise in tests

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400); self.end_headers()
            return
        method = req.get("method")
        req_id = req.get("id")
        result = self._response_for(method, req.get("params") or {})
        resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        data = json.dumps(resp).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _response_for(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "fake", "version": "1.0"},
                    "capabilities": {}}
        if method == "tools/list":
            return {"tools": [
                {"name": "ping", "description": "health check",
                 "inputSchema": {"type": "object",
                                 "properties": {"who": {"type": "string"}}}},
            ]}
        if method == "tools/call":
            args = params.get("arguments") or {}
            return {"content": [{"type": "text",
                                 "text": f"pong:{args.get('who', 'ping')}"}]}
        return {}


@pytest.fixture
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeStreamableHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_mcp_client_initialize_and_call(http_server):
    client = HttpMCPClient("fake", http_server,
                           headers={"Authorization": "Bearer test"})
    client.startup()
    try:
        assert [t["name"] for t in client.tools] == ["ping"]
        result = client.call_tool("ping", {"who": "world"})
        assert "pong:world" in result
    finally:
        client.close()


def test_http_mcp_client_bad_url_raises():
    client = HttpMCPClient("bad", "http://127.0.0.1:1/mcp")
    with pytest.raises(HttpMCPError):
        client.startup()


def test_http_mcp_client_missing_url_raises():
    with pytest.raises(HttpMCPError):
        HttpMCPClient("x", "")


def test_pool_connect_http_via_spec(http_server):
    pool = MCPPool("p")
    ok, msg = pool.connect_from_spec("fake", {
        "type": "http", "url": http_server,
    })
    assert ok is True, msg
    tools = pool.all_tools()
    assert any(t.name == "mcp__fake__ping" for t in tools)


# ── SSE smoke (just constructor + missing-endpoint timeout) ────────────

def test_sse_mcp_client_missing_url_raises():
    with pytest.raises(HttpMCPError):
        SseMCPClient("x", "")
