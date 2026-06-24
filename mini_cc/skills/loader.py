"""Per-project skill loader.

Scans the project's plugin tiers (system → tenant → project) and merges
them with project-tier winning on name conflicts. Each tier contributes
skills from ``<tier_dir>/.mini_cc/skills/<name>/SKILL.md``.

Legacy ``<project_root>/skills/`` layout is still scanned as a final
fallback so existing deployments don't break; new installs should use
``<project_root>/.mini_cc/skills/``.
"""
from __future__ import annotations

from pathlib import Path

from ..plugins import (PluginTier, Skill as _PSkill, discover_skills,
                       ensure_tier_dir, tier_dir)


class SkillLoader:
    def __init__(self, project_root: Path,
                 tier_dirs: list[Path] | None = None,
                 data_dir: Path | None = None,
                 tenant_id: str | None = None):
        """Build a loader.

        - ``tier_dirs`` (preferred): explicit list of ``.mini_cc/`` dirs
          in priority order (system first, project last).
        - ``data_dir + tenant_id`` (convenience): builds the standard
          3-tier list via :func:`project_tier_dirs`.
        - Neither: legacy single-dir behavior — scan
          ``<project_root>/skills/`` only.
        """
        self.project_root = Path(project_root)
        self._tier_dirs: list[Path] | None = tier_dirs
        self._data_dir = Path(data_dir) if data_dir else None
        self._tenant_id = tenant_id
        self._legacy_skills_dir = self.project_root / "skills"
        self._registry: dict[str, _PSkill] = {}

    def _resolve_tier_dirs(self) -> list[Path]:
        if self._tier_dirs is not None:
            return self._tier_dirs
        if self._data_dir is not None and self._tenant_id is not None:
            return [
                tier_dir(self._data_dir, PluginTier.SYSTEM),
                tier_dir(self._data_dir, PluginTier.TENANT,
                         tenant_id=self._tenant_id),
                tier_dir(self.project_root, PluginTier.PROJECT),
            ]
        return []  # legacy single-dir mode

    def scan(self) -> None:
        """Rebuild the registry from disk."""
        self._registry.clear()
        tier_dirs = self._resolve_tier_dirs()
        if tier_dirs:
            self._registry.update(discover_skills(tier_dirs))
        # Legacy fallback: <project_root>/skills/ (s20-style layout).
        # Modern installs put skills under .mini_cc/skills/ instead.
        if self._legacy_skills_dir.exists():
            self._registry.update(
                discover_skills([self._legacy_skills_dir.parent]))

    @property
    def registry(self) -> dict[str, _PSkill]:
        if not self._registry:
            self.scan()
        return self._registry

    def catalog(self) -> str:
        """One-line-per-skill listing for the system prompt."""
        if not self.registry:
            return "(no skills found)"
        return "\n".join(f"- {s.name}: {s.description}"
                         for s in self._registry.values())

    def load(self, name: str) -> str:
        skill = self.registry.get(name)
        if skill is None:
            available = ", ".join(self._registry.keys()) or "(none)"
            return f"Skill not found: {name}. Available: {available}"
        return skill.content
