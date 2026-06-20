"""Per-project skill loader.

Scans <project_root>/skills/*/SKILL.md, parses YAML frontmatter, exposes
a catalog string for the system prompt and a load_skill(name) accessor.

Ports s20 lines 285-341 with these differences:
- No module-level SKILL_REGISTRY global — instance state.
- SKILLS_DIR derived from the project_root passed at construction.
- scan() is explicit (not run at import time).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    content: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


class SkillLoader:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.skills_dir = self.project_root / "skills"
        self._registry: dict[str, Skill] = {}

    def scan(self) -> None:
        """Rebuild the registry from disk."""
        self._registry.clear()
        if not self.skills_dir.exists():
            return
        for directory in sorted(self.skills_dir.iterdir()):
            if not directory.is_dir():
                continue
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", directory.name)
            desc = meta.get("description") or raw.split("\n", 1)[0].lstrip("#").strip()
            self._registry[name] = Skill(name=name, description=desc, content=raw)

    @property
    def registry(self) -> dict[str, Skill]:
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
