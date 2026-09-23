"""Tests for ProjectManager project caching (perf + correctness fix).

Before this fix, ``ProjectManager.get()`` re-assembled the whole Project
on every call — re-running the MCP auto-connect sweep (~2s for a remote
HTTP server) and spawning a fresh, never-closed MCPPool each time. Every
API route calls ``pm.get()``, so a page refresh (5+ parallel calls) plus
the 3s permission poller turned into a continuous ~2s/request storm, and
leaked MCP subprocesses/connections. Caching fixes the perf, the leak,
and a latent identity bug where routes and the warm session held
*different* Project objects.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from mini_cc.projects import ProjectManager


# ── Identity / caching ───────────────────────────────────────────────

def test_get_returns_cached_project_object(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    first = pm.get("p", tenant_id="t")
    second = pm.get("p", tenant_id="t")
    assert first is second  # same object — no re-assembly


def test_get_resolves_tenant_then_caches(tmp_path):
    """get_any() resolves via find_meta then delegates to the tenant-
    scoped get(), so the cache lands under the resolved (tenant, pid)
    key and the tenant-scoped call returns the same identity."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    first = pm.get_any("p")       # explicit cross-tenant path
    second = pm.get("p", tenant_id="t")
    assert first is second


def test_get_does_not_reassemble_mcp_each_call(tmp_path):
    """The cached MCPPool identity must be stable across get() calls —
    proves we're not spawning a fresh pool + reconnect sweep."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    p1 = pm.get("p", tenant_id="t")
    p2 = pm.get("p", tenant_id="t")
    assert p1.mcp_pool is p2.mcp_pool


# ── Config-change invalidation ───────────────────────────────────────

def test_get_reassembles_when_mcp_json_changes(tmp_path):
    """Editing .mini_cc/.mcp.json after the first get() must invalidate
    the cache so a freshly-added MCP server is picked up on next get()."""
    pm = ProjectManager(tmp_path, data_dir_for_system=tmp_path)
    p = pm.create(tenant_id="t", project_id="p")
    first = pm.get("p", tenant_id="t")

    # Bump mtime on the project-tier mcp config so the cache sees a change.
    cfg = p.workspace / ".mini_cc" / ".mcp.json"
    cfg.write_text('{"mcpServers": {"x": {"command": "echo"}}}', encoding="utf-8")
    # Ensure mtime actually advances (some filesystems have 1s granularity).
    new_mtime = time.time() + 5
    os.utime(cfg, (new_mtime, new_mtime))

    second = pm.get("p", tenant_id="t")
    assert second is not first  # re-assembled because config changed


def test_get_stable_when_config_unchanged(tmp_path):
    pm = ProjectManager(tmp_path, data_dir_for_system=tmp_path)
    pm.create(tenant_id="t", project_id="p")
    first = pm.get("p", tenant_id="t")
    # No config edits → same object.
    assert pm.get("p", tenant_id="t") is first


# ── Explicit invalidation ────────────────────────────────────────────

def test_invalidate_drops_cache_entry(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    first = pm.get("p", tenant_id="t")
    pm.invalidate("p", tenant_id="t")
    assert pm.get("p", tenant_id="t") is not first


def test_delete_invalidates_cache(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    pm.get("p", tenant_id="t")
    pm.delete("p", tenant_id="t")
    # Cache must not keep a handle to the deleted project's paths.
    assert ("t", "p") not in pm._cache


# ── Perf: second call is fast ─────────────────────────────────────────

def test_second_get_is_microseconds_not_seconds(tmp_path):
    """Regression guard: the whole reason for caching. The cached path
    must be O(microseconds), not O(seconds)."""
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t", project_id="p")
    pm.get("p", tenant_id="t")  # prime
    t0 = time.perf_counter()
    for _ in range(100):
        pm.get("p", tenant_id="t")
    elapsed = time.perf_counter() - t0
    # 100 cached lookups must complete well under 1s (config-stat is cheap).
    assert elapsed < 1.0, f"cached get too slow: {elapsed:.3f}s/100 calls"
