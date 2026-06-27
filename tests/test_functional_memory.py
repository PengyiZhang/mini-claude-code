"""F1.2: 3-tier declarative memory (write/list/recall).

Layout (priority low → high):
  <data>/.mini_cc/memory/<name>.md           # system tier
  <data>/tenants/<tid>/.mini_cc/memory/...   # tenant tier
  <workspace>/.mini_cc/memory/<name>.md      # project tier (write target)

Each memory file is a markdown doc with optional frontmatter
(`name`, `description`, `category`). Later tiers override earlier on
name clash (same merge rule as skills/MCP).
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── discovery ────────────────────────────────────────────────────────────

def test_discover_memories_merges_three_tiers(tmp_path):
    """Project tier overrides tenant, tenant overrides system."""
    from mini_cc.plugins.discover import discover_memories
    sys_ = tmp_path / "sys" / ".mini_cc" / "memory"
    ten_ = tmp_path / "ten" / ".mini_cc" / "memory"
    proj = tmp_path / "proj" / ".mini_cc" / "memory"
    for p in (sys_, ten_, proj):
        p.mkdir(parents=True)
    (sys_ / "coding-style.md").write_text("---\nname: coding-style\n"
                                          "description: sys default\n---\n"
                                          "Use 2-space indent.")
    (ten_ / "coding-style.md").write_text("---\nname: coding-style\n"
                                          "description: tenant override\n---\n"
                                          "Use 4-space indent.")
    (proj / "naming.md").write_text("---\nname: naming\n"
                                    "description: project rule\n---\n"
                                    "Use snake_case.")

    tiers = [tmp_path / "sys" / ".mini_cc",
             tmp_path / "ten" / ".mini_cc",
             tmp_path / "proj" / ".mini_cc"]
    out = discover_memories(tiers)
    assert set(out.keys()) == {"coding-style", "naming"}
    # Tenant wins over system
    assert "4-space" in out["coding-style"].content
    assert "2-space" not in out["coding-style"].content


def test_discover_memories_handles_missing_dirs(tmp_path):
    """No tier dir → empty dict, no exception."""
    from mini_cc.plugins.discover import discover_memories
    out = discover_memories([tmp_path / "does-not-exist"])
    assert out == {}


def test_discover_memories_skips_non_md(tmp_path):
    """README.md etc. under memory/ are still .md; only files are scanned,
    subdirectories are skipped. Name comes from frontmatter or filename."""
    from mini_cc.plugins.discover import discover_memories
    d = tmp_path / ".mini_cc" / "memory"
    d.mkdir(parents=True)
    (d / "rule1.md").write_text("Just a body, no frontmatter.")
    (d / "notes.txt").write_text("ignored")
    sub = d / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("ignored")
    out = discover_memories([tmp_path / ".mini_cc"])
    assert list(out.keys()) == ["rule1"]
    assert "no frontmatter" in out["rule1"].content


# ── MemoryLoader ────────────────────────────────────────────────────────

def test_memory_loader_catalog_and_get(tmp_path):
    """Loader scans 3 tiers and exposes catalog/get like SkillLoader."""
    from mini_cc.memory import MemoryLoader
    d = tmp_path / ".mini_cc" / "memory"
    d.mkdir(parents=True)
    (d / "style.md").write_text("---\nname: style\ndescription: code style\n---\n"
                                "4-space indent.")
    loader = MemoryLoader(project_root=tmp_path,
                          tier_dirs=[tmp_path / ".mini_cc"])
    catalog = loader.catalog()
    assert "style" in catalog
    assert "code style" in catalog
    body = loader.get("style")
    assert "4-space" in body


def test_memory_loader_get_missing_returns_helpful_message(tmp_path):
    from mini_cc.memory import MemoryLoader
    loader = MemoryLoader(project_root=tmp_path,
                          tier_dirs=[tmp_path / ".mini_cc"])
    msg = loader.get("nope")
    assert "not found" in msg.lower()


# ── write tool (project tier only) ──────────────────────────────────────

def test_memory_write_persists_to_project_tier(tmp_path):
    """memory_write saves a new .md file under <workspace>/.mini_cc/memory/."""
    from mini_cc.memory import write_memory
    write_memory(tmp_path, "decision-1",
                 "Use Postgres not MySQL", category="decisions")
    fp = tmp_path / ".mini_cc" / "memory" / "decision-1.md"
    assert fp.exists()
    raw = fp.read_text(encoding="utf-8")
    assert "Postgres" in raw
    assert "decisions" in raw  # category in frontmatter


def test_memory_write_sanitizes_name(tmp_path):
    """Name with slashes/spaces must not escape the memory dir."""
    from mini_cc.memory import write_memory
    write_memory(tmp_path, "../escape", "evil")
    # File written under memory/ with sanitized name; nothing escaped
    written = list((tmp_path / ".mini_cc" / "memory").iterdir())
    assert len(written) == 1
    assert ".." not in written[0].name
    assert (tmp_path / "..").resolve() != written[0].resolve().parent.parent


# ── recall (keyword search) ─────────────────────────────────────────────

def test_memory_recall_matches_keyword_across_files(tmp_path):
    from mini_cc.memory import MemoryLoader, write_memory
    write_memory(tmp_path, "dec-1", "We chose Postgres for persistence.")
    write_memory(tmp_path, "dec-2", "Frontend uses React.")
    write_memory(tmp_path, "issue-1", "Postgres connection pool exhausted.")
    loader = MemoryLoader(project_root=tmp_path,
                          tier_dirs=[tmp_path / ".mini_cc"])
    matches = loader.recall("Postgres")
    names = [m.name for m in matches]
    assert "dec-1" in names
    assert "issue-1" in names
    assert "dec-2" not in names


def test_memory_recall_empty_when_no_match(tmp_path):
    from mini_cc.memory import MemoryLoader
    loader = MemoryLoader(project_root=tmp_path,
                          tier_dirs=[tmp_path / ".mini_cc"])
    assert loader.recall("nonexistent") == []
