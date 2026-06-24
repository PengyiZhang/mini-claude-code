"""3-tier plugin discovery: system / tenant / project.

Re-exports the path helpers + the skill/mcp discovery loaders. Code that
previously reached into the per-project ``SkillLoader`` or read
``MINI_CC_MCP_SERVERS`` directly should switch to
:func:`project_tier_dirs` + :func:`discover_skills` / :func:`discover_mcp_servers`.
"""
from .discover import (Skill, discover_mcp_servers, discover_skills,
                       write_mcp_servers)
from .paths import (PluginTier, ensure_tier_dir, project_tier_dirs,
                    tier_dir)

__all__ = [
    "PluginTier",
    "Skill",
    "tier_dir",
    "ensure_tier_dir",
    "project_tier_dirs",
    "discover_skills",
    "discover_mcp_servers",
    "write_mcp_servers",
]
