"""Async MCP connect — servers warm up in background, project loads instantly.

Before this change, project assembly called ``connect_from_spec``
synchronously for every entry in ``.mcp.json``. With the 60s cold-start
timeout (needed for ``npx -y <pkg>`` downloads), a project with three
slow MCP servers blocked load for up to 3 minutes.

The fix: ``connect_from_spec_async`` dispatches the underlying connect
to a background thread. It returns immediately after recording the
server in ``_connecting`` so ``/mcp`` can render a 'connecting' badge.
On completion (success or failure) the server moves out of
``_connecting`` into the same connected/failed state the sync path
produces — so the existing roster rendering keeps working once the
connect finishes.
"""
from __future__ import annotations

import threading
import time

import pytest

from mini_cc.mcp import MCPPool


@pytest.fixture(autouse=True)
def _reset_factories():
    MCPPool.reset_factories()
    yield
    MCPPool.reset_factories()


# ── Async dispatch ────────────────────────────────────────────────────


def _slow_transport(monkeypatch, *, delay: float, fail: bool = False):
    """Replace stdio/http transports with stubs that take ``delay``
    seconds to start. Lets us assert the connect happens off the
    caller's thread without relying on real subprocess timing."""
    from mini_cc.mcp.stdio import StdioMCPError
    from mini_cc.mcp.http import HttpMCPError

    class _SlowStdio:
        def __init__(self, *a, **kw):
            self.tools = []

        def startup(self):
            time.sleep(delay)
            if fail:
                raise StdioMCPError("stub stdio failure")

        def close(self):
            pass

    class _SlowHttp:
        def __init__(self, *a, **kw):
            self.tools = []

        def startup(self):
            time.sleep(delay)
            if fail:
                raise HttpMCPError("stub http failure")

        def close(self):
            pass

    monkeypatch.setattr("mini_cc.mcp.stdio.StdioMCPClient", _SlowStdio)
    monkeypatch.setattr("mini_cc.mcp.http.HttpMCPClient", _SlowHttp)


def test_connect_from_spec_async_returns_immediately(monkeypatch):
    """The whole point: a slow server must NOT block the caller. Pre-fix
    this test would take ``delay`` seconds; post-fix it returns in <100ms."""
    _slow_transport(monkeypatch, delay=2.0)
    pool = MCPPool("p")
    t0 = time.monotonic()
    pool.connect_from_spec_async(
        "slow", {"type": "http", "url": "http://x.invalid/mcp"})
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, (
        f"async dispatch must return immediately; took {elapsed:.2f}s")
    # The server is registered as connecting right away so /mcp can
    # show a spinner instead of looking like nothing happened.
    assert "slow" in pool.list_connecting()


def test_async_connect_completes_to_connected(monkeypatch):
    """After the background thread finishes, the server moves out of
    _connecting and into _clients — same end state as the sync path."""
    _slow_transport(monkeypatch, delay=0.05)
    pool = MCPPool("p")
    pool.connect_from_spec_async(
        "quick", {"type": "http", "url": "http://x.invalid/mcp"})
    assert pool.wait_for_connect("quick", timeout=2.0), (
        "background connect did not complete in time")
    assert "quick" not in pool.list_connecting()
    assert "quick" in pool.list_connected()


def test_async_connect_failure_records_attempt(monkeypatch):
    """A failed background connect must still surface in /mcp's failed
    list — same contract as sync. User needs to see WHY it failed and
    have a reconnect path."""
    _slow_transport(monkeypatch, delay=0.05, fail=True)
    pool = MCPPool("p")
    pool.connect_from_spec_async(
        "bad", {"type": "http", "url": "http://x.invalid/mcp"})
    assert pool.wait_for_connect("bad", timeout=2.0)
    assert "bad" not in pool.list_connecting()
    assert "bad" not in pool.list_connected()
    attempts = pool.list_attempts()
    assert "bad" in attempts
    assert attempts["bad"].ok is False
    assert "stub http failure" in attempts["bad"].message


def test_async_connect_does_not_clobber_existing_client(monkeypatch):
    """If the name is already connected, async dispatch is a no-op
    success — matches the sync contract."""
    _slow_transport(monkeypatch, delay=0.05)
    pool = MCPPool("p")
    pool.connect_from_spec("first",
                           {"type": "http", "url": "http://a.invalid/mcp"})
    pool.connect_from_spec_async("first",
                                 {"type": "http",
                                  "url": "http://b.invalid/mcp"})
    assert pool.wait_for_connect("first", timeout=1.0)
    # Still the original URL; the second async dispatch didn't replace it.
    assert pool.get_spec("first")["url"] == "http://a.invalid/mcp"


def test_wait_for_connect_times_out(monkeypatch):
    """If the background connect hasn't finished, wait_for_connect
    returns False instead of hanging the caller."""
    _slow_transport(monkeypatch, delay=2.0)
    pool = MCPPool("p")
    pool.connect_from_spec_async(
        "slow", {"type": "http", "url": "http://x.invalid/mcp"})
    assert pool.wait_for_connect("slow", timeout=0.1) is False
    # Still in flight — not silently dropped.
    assert "slow" in pool.list_connecting()


# ── Roster shows connecting badge ─────────────────────────────────────


# ── Thread safety ─────────────────────────────────────────────────────


def test_concurrent_async_connects_all_land(monkeypatch):
    """Multiple slow servers dispatched in parallel must all complete
    and land in _clients — the pool's internal lock has to serialize
    mutations without blocking the dispatch path."""
    _slow_transport(monkeypatch, delay=0.2)
    pool = MCPPool("p")
    for name in ("a", "b", "c", "d"):
        pool.connect_from_spec_async(
            name, {"type": "http", "url": f"http://{name}.invalid/mcp"})
    # All four should complete well under 4 * 0.2s since they run in
    # parallel.
    for name in ("a", "b", "c", "d"):
        assert pool.wait_for_connect(name, timeout=3.0), (
            f"{name} didn't connect in time")
    assert set(pool.list_connected()) == {"a", "b", "c", "d"}
