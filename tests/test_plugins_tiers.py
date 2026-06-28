"""3-tier plugin discovery — system / tenant / project.

Each tier's ``.mini_cc/`` directory has the same shape::

    .mini_cc/
      skills/
        <skill-name>/SKILL.md
      mcp.toml
      permissions.toml

Discovery walks tiers system → tenant → project. Later tiers add new
entries; same-named entries override earlier ones (project > tenant >
system). MCP ``mcp.toml`` merges the same way; the legacy
``MINI_CC_MCP_SERVERS`` env var still works and overrides disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.plugins import (
    PluginTier,
    tier_dir,
    ensure_tier_dir,
    discover_skills,
    discover_mcp_servers,
    write_mcp_servers,
)
from mini_cc.plugins.discover import Skill


# ── tier_dir + ensure_tier_dir ────────────────────────────────────────

def test_tier_dir_system(tmp_path):
    assert tier_dir(tmp_path, PluginTier.SYSTEM) == tmp_path / ".mini_cc"


def test_tier_dir_tenant(tmp_path):
    assert tier_dir(tmp_path, PluginTier.TENANT, tenant_id="t1") == (
        tmp_path / "tenants" / "t1" / ".mini_cc")


def test_tier_dir_project(tmp_path):
    ws = tmp_path / "ws"
    assert tier_dir(ws, PluginTier.PROJECT) == ws / ".mini_cc"


def test_tier_dir_tenant_requires_tid(tmp_path):
    with pytest.raises(ValueError):
        tier_dir(tmp_path, PluginTier.TENANT)


def test_ensure_tier_dir_creates_skills_subdir(tmp_path):
    td = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    assert (td / "skills").is_dir()


def test_ensure_tier_dir_idempotent(tmp_path):
    ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    ensure_tier_dir(tmp_path, PluginTier.SYSTEM)  # no error
    assert (tmp_path / ".mini_cc" / "skills").is_dir()


def test_ensure_tier_dir_rejects_unsafe_tenant(tmp_path):
    with pytest.raises(ValueError):
        ensure_tier_dir(tmp_path, PluginTier.TENANT, tenant_id="../x")


# ── Skill discovery ───────────────────────────────────────────────────

def _write_skill(base: Path, name: str, description: str,
                 body: str = "skill body") -> None:
    sk_dir = base / "skills" / name
    sk_dir.mkdir(parents=True, exist_ok=True)
    (sk_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8")


def test_discover_skills_picks_up_single_tier(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_skill(sys_dir, "alpha", "system skill")
    out = discover_skills([sys_dir])
    assert set(out) == {"alpha"}
    assert out["alpha"].description == "system skill"


def test_discover_skills_picks_up_nested_category_dirs(tmp_path):
    """Regression: third-party skill packs (superpowers etc.) ship in
    a nested layout — skills/<category>/<name>/SKILL.md. The scanner
    previously used iterdir() one level deep and missed everything
    under a category subdir."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    # Nested layout (the bug)
    for path, name, desc in [
        ("architecture/tensions", "tensions", "arch skill"),
        ("collaboration/brainstorming", "brainstorming", "collab"),
        ("debugging/systematic", "systematic-debugging", "debug"),
    ]:
        d = sys_dir / "skills" / path
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody\n",
            encoding="utf-8")
    # Flat layout still works alongside nested.
    _write_skill(sys_dir, "flat", "flat skill")

    out = discover_skills([sys_dir])
    assert set(out) == {
        "tensions", "brainstorming", "systematic-debugging", "flat"}
    assert out["tensions"].description == "arch skill"
    assert out["brainstorming"].description == "collab"


def test_discover_skills_nested_name_defaults_to_parent_dir(tmp_path):
    """When frontmatter omits ``name``, the skill id falls back to the
    SKILL.md's parent directory name (NOT a higher category dir)."""
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    d = sys_dir / "skills" / "categoryA" / "real-name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\ndescription: no name in frontmatter\n---\nbody\n",
        encoding="utf-8")
    out = discover_skills([sys_dir])
    assert "real-name" in out
    assert "categoryA" not in out


def test_discover_skills_merges_tiers(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    ten_dir = ensure_tier_dir(tmp_path / "tenants" / "t1",
                              PluginTier.TENANT, tenant_id="t1")
    proj_dir = ensure_tier_dir(tmp_path / "ws", PluginTier.PROJECT)

    _write_skill(sys_dir, "alpha", "from-system")
    _write_skill(sys_dir, "beta", "from-system")
    _write_skill(ten_dir, "beta", "from-tenant")
    _write_skill(ten_dir, "gamma", "from-tenant")
    _write_skill(proj_dir, "delta", "from-project")

    out = discover_skills([sys_dir, ten_dir, proj_dir])
    # All four names present.
    assert set(out) == {"alpha", "beta", "gamma", "delta"}
    # Project & tenant override system for the same name.
    assert out["beta"].description == "from-tenant"
    assert out["alpha"].description == "from-system"


def test_discover_skills_project_overrides_tenant(tmp_path):
    ten_dir = ensure_tier_dir(tmp_path / "t", PluginTier.TENANT,
                              tenant_id="t")
    proj_dir = ensure_tier_dir(tmp_path / "ws", PluginTier.PROJECT)
    _write_skill(ten_dir, "shared", "tenant-version")
    _write_skill(proj_dir, "shared", "project-version")
    out = discover_skills([ten_dir, proj_dir])
    assert out["shared"].description == "project-version"


def test_discover_skills_empty_dirs_return_empty(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    out = discover_skills([sys_dir])
    assert out == {}


def test_discover_skills_skips_dirs_without_manifest(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    # A skill dir without SKILL.md should be skipped, not crash.
    (sys_dir / "skills" / "incomplete").mkdir(parents=True)
    out = discover_skills([sys_dir])
    assert out == {}


# ── MCP discovery ─────────────────────────────────────────────────────

def test_write_then_discover_mcp_servers(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    write_mcp_servers(sys_dir, {
        "docs": {"command": ["npx", "mcp-server-docs"]},
    })
    out = discover_mcp_servers([sys_dir])
    assert "docs" in out
    assert out["docs"]["command"] == ["npx", "mcp-server-docs"]


def test_discover_mcp_merges_across_tiers(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    ten_dir = ensure_tier_dir(tmp_path / "t", PluginTier.TENANT,
                              tenant_id="t")
    proj_dir = ensure_tier_dir(tmp_path / "ws", PluginTier.PROJECT)

    write_mcp_servers(sys_dir, {"docs": {"command": ["sys-docs"]}})
    write_mcp_servers(ten_dir, {"docs": {"command": ["ten-docs"]},
                                "fs": {"command": ["ten-fs"]}})
    write_mcp_servers(proj_dir, {"fs": {"command": ["proj-fs"]}})

    out = discover_mcp_servers([sys_dir, ten_dir, proj_dir])
    # Project & tenant override system, system-only survives.
    assert out["docs"]["command"] == ["ten-docs"]  # tenant wins
    assert out["fs"]["command"] == ["proj-fs"]     # project wins


def test_discover_mcp_handles_missing_files(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    # No mcp.toml anywhere → empty dict, no error.
    out = discover_mcp_servers([sys_dir])
    assert out == {}


def test_discover_mcp_malformed_file_skipped(tmp_path):
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    (sys_dir / "mcp.toml").write_text("not = valid = toml = [[", encoding="utf-8")
    out = discover_mcp_servers([sys_dir])
    assert out == {}


# ── ProjectManager integration ────────────────────────────────────────

def test_pm_create_bootstraps_project_mini_cc(tmp_path):
    """ProjectManager.create must materialize <workspace>/.mini_cc/skills/."""
    from mini_cc.projects import ProjectManager
    pm = ProjectManager(tmp_path)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert (p.workspace / ".mini_cc" / "skills").is_dir()


def test_pm_create_bootstraps_tenant_mini_cc(tmp_path):
    """ProjectManager.create must also materialize the tenant's .mini_cc/
    when the tenant is first seen (so admin can drop system/tenant-wide
    skills + mcp.toml without manual setup)."""
    from mini_cc.projects import ProjectManager
    pm = ProjectManager(tmp_path)
    pm.create(tenant_id="t1", project_id="p1")
    assert (tmp_path / "tenants" / "t1" / ".mini_cc" / "skills").is_dir()


def test_pm_assemble_loads_skills_from_all_three_tiers(tmp_path):
    """A skill placed at the system tier should be visible from a
    freshly-assembled project, alongside project-tier skills."""
    from mini_cc.config import set_default_config, AnthropicConfig
    from mini_cc.projects import ProjectManager

    # Write a system-tier skill.
    sys_dir = ensure_tier_dir(tmp_path, PluginTier.SYSTEM)
    _write_skill(sys_dir, "global-helper", "from-system")

    pm = ProjectManager(tmp_path, data_dir_for_system=tmp_path)
    # ^ see ProjectManager signature change — accepts an optional system
    # data_dir so it can locate <data>/.mini_cc/
    p = pm.create(tenant_id="t1", project_id="p1")
    _write_skill(Path(p.workspace) / ".mini_cc", "local", "from-project")
    p.rescan_skills()

    catalog = p.skills_loader.catalog()
    assert "global-helper" in catalog
    assert "local" in catalog


# ── Backward-compat: env MINI_CC_MCP_SERVERS still works ──────────────

def test_env_mcp_servers_picked_up_via_default_config(tmp_path, monkeypatch):
    """The env var path is the legacy escape hatch — verify it still flows
    into AnthropicConfig.mcp_servers so existing deployments don't break."""
    import json
    monkeypatch.setenv("MINI_CC_MCP_SERVERS", json.dumps({
        "legacy": {"command": ["legacy-bin"]},
    }))
    from mini_cc.config import AnthropicConfig
    cfg = AnthropicConfig.from_env()
    assert cfg.mcp_servers == {"legacy": {"command": ["legacy-bin"]}}
