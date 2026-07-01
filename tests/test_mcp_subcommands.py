"""Task 2 (debug.7.md): /mcp subcommands aligned with Claude Code.

Subcommands:
- ``/mcp`` (no args): existing roster card
- ``/mcp tools [server]``: list tools (all or filtered to one server)
- ``/mcp connect <name>``: invoke pool.connect(name)
- ``/mcp disconnect <name>``: invoke pool.disconnect(name)
- ``/mcp reconnect <name>``: disconnect then connect
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mini_cc.commands import CommandContext, default_registry


def _run(name, *, project=None, args=""):
    reg = default_registry()
    cmd = reg.resolve(name)
    assert cmd is not None
    ctx = CommandContext(
        project_id="p", session_id="s", tenant_id="t",
        args=args, project=project,
    )
    return list(cmd.handler(ctx))


@dataclass
class _Attempt:
    ok: bool = True
    message: str = "ok"


@dataclass
class _Client:
    name: str
    tools: list[dict] = field(default_factory=list)


class _Pool:
    """Fake pool that records every connect/disconnect call."""

    def __init__(self):
        self._clients: dict[str, _Client] = {}
        self._attempts: dict[str, _Attempt] = {}
        self.connect_calls: list[str] = []
        self.disconnect_calls: list[str] = []

    def list_connected(self):
        return sorted(self._clients.keys())

    def list_attempts(self):
        return dict(self._attempts)

    @classmethod
    def available_servers(cls):
        return []  # populated per-test via register_factory mock if needed

    def connect(self, name):
        self.connect_calls.append(name)
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        self._clients[name] = _Client(name, tools=[
            {"name": "tool_a", "description": "does A",
             "inputSchema": {"type": "object"}},
        ])
        self._attempts[name] = _Attempt(ok=True, message="connected")
        return True, f"MCP server '{name}' connected"

    def disconnect(self, name):
        self.disconnect_calls.append(name)
        if name not in self._clients:
            return False
        del self._clients[name]
        return True

    def all_tools(self):
        out = []
        for server, client in self._clients.items():
            for t in client.tools:
                out.append({"server": server, **t})
        return out


class _P:
    def __init__(self, pool):
        self.mcp_pool = pool
        self.tenant_id = "t1"


# ── /mcp tools ───────────────────────────────────────────────────────────


def test_mcp_tools_lists_all_connected_servers_tools():
    pool = _Pool()
    pool.connect("docs")
    pool.connect("search")
    events = _run("mcp", project=_P(pool), args="tools")
    card = next(e for e in events if e.get("type") == "card")
    blob = repr(card["payload"])
    assert "docs" in blob
    assert "search" in blob
    assert "tool_a" in blob


def test_mcp_tools_filtered_to_one_server():
    pool = _Pool()
    pool.connect("docs")
    pool.connect("search")
    # give 'search' a distinct tool
    pool._clients["search"].tools.append(
        {"name": "tool_b", "description": "does B",
         "inputSchema": {"type": "object"}})
    events = _run("mcp", project=_P(pool), args="tools docs")
    card = next(e for e in events if e.get("type") == "card")
    blob = repr(card["payload"])
    assert "docs" in blob
    assert "tool_a" in blob
    # 'search' server's tools filtered out
    assert "tool_b" not in blob


def test_mcp_tools_unknown_server_emits_empty_hint():
    pool = _Pool()
    pool.connect("docs")
    events = _run("mcp", project=_P(pool), args="tools ghost")
    card = next(e for e in events if e.get("type") == "card")
    items = card["payload"]["items"]
    assert items == []
    hint = (card["payload"].get("empty_hint") or "").lower()
    assert "ghost" in hint or "no such" in hint or "unknown" in hint


def test_mcp_tools_with_no_servers_connected():
    pool = _Pool()
    events = _run("mcp", project=_P(pool), args="tools")
    card = next(e for e in events if e.get("type") == "card")
    assert card["payload"]["items"] == []
    assert card["payload"].get("empty_hint")


# ── /mcp connect ────────────────────────────────────────────────────────


def test_mcp_connect_invokes_pool_connect():
    pool = _Pool()
    events = _run("mcp", project=_P(pool), args="connect docs")
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    blob = text + repr(events)
    assert "docs" in blob
    assert "connect" in blob.lower() or "ok" in blob.lower()
    assert pool.connect_calls == ["docs"]


def test_mcp_connect_missing_name_errors():
    pool = _Pool()
    events = _run("mcp", project=_P(pool), args="connect")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "usage" in err["message"].lower() or "name" in err["message"].lower()


def test_mcp_connect_failure_surfaces_error():
    """If pool.connect returns (False, reason), the command surfaces the
    reason in an error event."""
    class _FailPool(_Pool):
        def connect(self, name):
            self.connect_calls.append(name)
            self._attempts[name] = _Attempt(ok=False, message="boom")
            return False, "boom"
    pool = _FailPool()
    events = _run("mcp", project=_P(pool), args="connect docs")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "boom" in err["message"]


# ── /mcp disconnect ─────────────────────────────────────────────────────


def test_mcp_disconnect_invokes_pool_disconnect():
    pool = _Pool()
    pool.connect("docs")
    events = _run("mcp", project=_P(pool), args="disconnect docs")
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    blob = text + repr(events)
    assert "docs" in blob
    assert "disconnect" in blob.lower() or "ok" in blob.lower()
    assert pool.disconnect_calls == ["docs"]


def test_mcp_disconnect_missing_name_errors():
    pool = _Pool()
    events = _run("mcp", project=_P(pool), args="disconnect")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None


def test_mcp_disconnect_unknown_returns_error():
    pool = _Pool()
    events = _run("mcp", project=_P(pool), args="disconnect ghost")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None


# ── /mcp reconnect ──────────────────────────────────────────────────────


def test_mcp_reconnect_disconnects_then_connects():
    pool = _Pool()
    pool.connect("docs")
    pool.connect_calls.clear()
    pool.disconnect_calls.clear()
    events = _run("mcp", project=_P(pool), args="reconnect docs")
    # disconnect first, then connect
    assert pool.disconnect_calls == ["docs"]
    assert pool.connect_calls == ["docs"]
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    assert "docs" in text


def test_mcp_reconnect_unknown_errors():
    """Reconnect surfaces an error when connect ultimately fails."""
    class _FailPool(_Pool):
        def connect(self, name):
            self.connect_calls.append(name)
            self._attempts[name] = _Attempt(ok=False, message="unknown server")
            return False, "unknown server"
    pool = _FailPool()
    events = _run("mcp", project=_P(pool), args="reconnect ghost")
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None
    assert "unknown server" in err["message"]
