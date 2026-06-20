"""Skills loader — per-project skill registry.

Ports s20 lines 285-341 from s20_comprehensive/code.py.
"""
from .loader import Skill, SkillLoader, _parse_frontmatter

__all__ = ["Skill", "SkillLoader"]
