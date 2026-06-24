"""Skills loader — per-project skill registry.

Ports s20 lines 285-341 from s20_comprehensive/code.py.
"""
from ..plugins.discover import Skill
from .loader import SkillLoader

__all__ = ["Skill", "SkillLoader"]
