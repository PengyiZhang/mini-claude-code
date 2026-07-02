"""debug.8 Task B: disconnect must preserve the server for reconnect.

Previously disconnect() removed the entry entirely from the pool. A server
discovered via ``.mcp.json`` (registered through ``connect_from_spec``)
lost all state, so a subsequent ``/mcp reconnect <name>`` failed with
"Unknown server 'tavily-remote-mcp'. Available: (none)" — the spec was
gone, and there was no way back short of restarting the server.

The fix: the pool tracks the spec for every spec-connected server in
``_specs``. ``disconnect()`` only drops the live client, not the spec.
``reconnect()`` re-runs ``connect_from_spec`` from the stored spec.
``/mcp`` shows disconnected-but-configured servers in a "disconnected"
state so the user can see them and click reconnect.
"""
from __future__ import annotations

import pytest

from mini_cc.commands.registry import CommandContext, _cmd_mcp
from mini_cc.mcp import MCPPool


@pytest.fixture(autouse=True)
def _clear_factories():
    MCPPool.reset_factories()
    yield
    MCPPool.reset_factories()


# ── Pool level: spec survives disconnect ──────────────────────────────


def _stub_transport(monkeypatch):
    """Replace stdio/http transports with in-memory stubs so we can test
    connect/disconnect/reconnect without spawning subprocesses or hitting
    the network."""
    class _FakeClient:
        def __init__(self, name):
            self.name = name
            self.tools = [{"name": "stub_tool", "description": "stub",
                           "inputSchema": {"type": "object"}}]
            self.closed = False

        def close(self):
            self.closed = True

    def fake_http(self, name, url, *, headers=None):
        self._clients[name] = _FakeClient(name)
        self._specs[name] = {"type": "http", "url": url, "headers": headers}
        self._record_attempt(name, True, "ok")
        return True, "ok"

    monkeypatch.setattr(MCPPool, "connect_http", fake_http)


def test_disconnect_then_reconnect_round_trip(monkeypatch):
    """connect_from_spec → disconnect → reconnect should succeed and
    leave the server usable. Pre-fix this fails with 'Unknown server'."""
    _stub_transport(monkeypatch)
    pool = MCPPool("p")
    pool.connect_from_spec("tavily",
                           {"type": "http", "url": "https://x.invalid/mcp"})
    assert "tavily" in pool.list_connected()

    assert pool.disconnect("tavily") is True
    # After disconnect: NOT in connected, but spec preserved.
    assert "tavily" not in pool.list_connected()
    assert "tavily" in pool.list_known_servers(), (
        "disconnect must preserve the spec so /mcp can show the server "
        "in a 'disconnected' state and reconnect can find it later")

    # reconnect path
    ok, msg = pool.reconnect("tavily")
    assert ok is True, f"reconnect failed: {msg}"
    assert "tavily" in pool.list_connected()


def test_reconnect_unknown_server_errors_with_helpful_message():
    """reconnect on a name we've never seen must still error — but the
    'Available:' list should now include disconnected-but-known servers."""
    pool = MCPPool("p")
    ok, msg = pool.reconnect("ghost")
    assert ok is False
    assert "ghost" in msg or "Unknown" in msg or "not found" in msg.lower()


def test_disconnect_drops_live_client_but_keeps_spec(monkeypatch):
    """Sanity: disconnect really tears down the live client (calls close)
    while the spec dict stays intact for the next connect attempt."""
    _stub_transport(monkeypatch)
    pool = MCPPool("p")
    pool.connect_from_spec("srv",
                           {"type": "http", "url": "https://x.invalid/mcp"})
    live = pool._clients["srv"]
    pool.disconnect("srv")
    assert live.closed is True
    assert "srv" not in pool._clients
    assert "srv" in pool._specs


def test_list_known_servers_includes_disconnected_and_connected(monkeypatch):
    """list_known_servers() = every server we have a spec for, regardless
    of live state. Used by /mcp roster to render the disconnected row."""
    _stub_transport(monkeypatch)
    pool = MCPPool("p")
    pool.connect_from_spec("a",
                           {"type": "http", "url": "https://a.invalid/mcp"})
    pool.connect_from_spec("b",
                           {"type": "http", "url": "https://b.invalid/mcp"})
    pool.disconnect("b")
    known = pool.list_known_servers()
    assert set(known) == {"a", "b"}


def test_factory_registered_servers_also_reconnectable(monkeypatch):
    """Servers registered via register_factory (the programmatic path,
    pre-dating .mcp.json) must also be reconnectable after disconnect."""
    _stub_transport(monkeypatch)

    class _Client:
        def __init__(self):
            self.tools = []

    MCPPool.register_factory("prog", lambda: _Client())
    pool = MCPPool("p")
    pool.connect("prog")
    pool.disconnect("prog")
    ok, msg = pool.reconnect("prog")
    assert ok is True, f"factory server reconnect failed: {msg}"
    assert "prog" in pool.list_connected()


# ── /mcp roster shows disconnected state ──────────────────────────────


def test_roster_shows_disconnected_state(monkeypatch):
    """After disconnect the server must still appear in /mcp, with a
    'disconnected' badge (not silently gone). Pre-fix the row vanished."""
    _stub_transport(monkeypatch)
    pool = MCPPool("p")
    pool.connect_from_spec("tavily",
                           {"type": "http", "url": "https://x.invalid/mcp"})
    pool.disconnect("tavily")

    class _Proj:
        mcp_pool = pool

    ctx = CommandContext(project_id="p", session_id="s", tenant_id="t",
                         project=_Proj())
    events = list(_cmd_mcp(ctx))
    card = next(e for e in events if e.get("type") == "card")
    items = card["payload"]["items"]
    names = {it["title"] for it in items}
    assert "tavily" in names, (
        "disconnected server vanished from /mcp — must still be visible")
    tav = next(it for it in items if it["title"] == "tavily")
    badges = {b["text"].lower() for b in tav["badges"]}
    assert "disconnected" in badges, (
        f"disconnected server must show 'disconnected' badge, got {badges}")
    menu_labels = {a["label"] for a in tav["menu"]}
    assert "reconnect" in menu_labels, (
        "disconnected server must offer a reconnect menu action")
