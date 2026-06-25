"""Tests for MCP connect-attempt visibility (root-cause fix).

Before this fix, a disk-discovered MCP server that FAILED to connect at
project-assembly time vanished completely: it was neither in
``connected`` nor in the ``connectable`` factory list, so ``/mcp`` said
"no MCP servers registered" even though the user had dropped a valid
``.mcp.json``. The fix tracks every connect *attempt* (name → status +
reason) on the pool and surfaces failures in ``/mcp``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.commands.registry import CommandContext, _cmd_mcp
from mini_cc.mcp import MCPPool


@pytest.fixture(autouse=True)
def _clear_factories():
    MCPPool.reset_factories()
    yield
    MCPPool.reset_factories()


# ── Pool records attempts ────────────────────────────────────────────

def test_connect_from_spec_records_failure():
    """A failed connect must be recorded so /mcp can show why it failed."""
    pool = MCPPool("p")
    ok, msg = pool.connect_http("tavily-remote", "http://127.0.0.1:1/mcp")
    assert ok is False
    attempts = pool.list_attempts()
    assert "tavily-remote" in attempts
    rec = attempts["tavily-remote"]
    assert rec.ok is False
    assert "401" in rec.message or "error" in rec.message.lower() or "refused" in rec.message.lower() or "network" in rec.message.lower()


def test_connect_from_spec_records_success(monkeypatch):
    pool = MCPPool("p")

    def fake_http(self, name, url, *, headers=None):
        self._record_attempt(name, True, "ok")
        self._clients[name] = type("C", (), {"tools": []})()
        return True, "ok"

    monkeypatch.setattr(MCPPool, "connect_http", fake_http)
    pool.connect_from_spec("good", {"type": "http", "url": "https://x"})
    attempts = pool.list_attempts()
    assert attempts["good"].ok is True


def test_list_attempts_empty_for_fresh_pool():
    assert MCPPool("p").list_attempts() == {}


# ── /mcp surfaces failures ────────────────────────────────────────────

def test_mcp_command_shows_failed_section():
    """The whole point: a discovered-but-failed server must appear in
    /mcp with its reason, not 'no MCP servers registered'."""

    class _FakePool:
        mcp_pool = None  # set below

    class _FakeProject:
        mcp_pool = None

    pool = MCPPool("p")
    # Simulate a failed auto-connect attempt recorded during assembly.
    pool._record_attempt("tavily-remote", False,
                         "MCP server `tavily-remote`: HTTP 401 Unauthorized")

    project = _FakeProject()
    project.mcp_pool = pool
    ctx = CommandContext(project_id="p", session_id="s", tenant_id="t",
                         project=project)
    events = list(_cmd_mcp(ctx))
    text = next(e["text"] for e in events if e.get("type") == "text")
    # Must NOT say "no MCP servers registered".
    assert "no MCP servers registered" not in text
    # Must name the failed server + its reason.
    assert "tavily-remote" in text
    assert "401" in text


# ── End-to-end through _connect_configured_mcp_servers ────────────────

def test_assembly_records_failed_attempt_from_disk(tmp_path, monkeypatch):
    """A .mcp.json pointing at an unreachable URL must leave a recorded
    failed attempt on the pool (visible via /mcp)."""
    from mini_cc.plugins import (PluginTier, ensure_tier_dir)
    from mini_cc.projects.manager import _connect_configured_mcp_servers

    proj_dir = ensure_tier_dir(tmp_path / "ws", PluginTier.PROJECT)
    (proj_dir / ".mcp.json").write_text(
        '{"mcpServers": {"dead": {"type": "http", '
        '"url": "http://127.0.0.1:1/mcp"}}}', encoding="utf-8")

    pool = MCPPool("p")
    _connect_configured_mcp_servers(
        pool, data_dir=tmp_path, tenant_id="t",
        workspace=tmp_path / "ws")
    attempts = pool.list_attempts()
    assert "dead" in attempts
    assert attempts["dead"].ok is False
