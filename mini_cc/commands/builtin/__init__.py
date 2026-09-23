"""Built-in slash command families (split from registry.py — M3-1)."""
from __future__ import annotations

from ..registry import CommandRegistry

from . import session_cmds
from . import skills_cmds
from . import project_cmds
from . import agents_cmds
from . import sched_cmds
from . import config_cmds
from . import workflow_cmds


def register_builtins(reg: CommandRegistry) -> None:
    session_cmds.register(reg)
    skills_cmds.register(reg)
    project_cmds.register(reg)
    agents_cmds.register(reg)
    sched_cmds.register(reg)
    config_cmds.register(reg)
    workflow_cmds.register(reg)
