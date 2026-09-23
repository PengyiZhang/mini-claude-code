"""Backward-compat shim: expose s20-style agent_loop(messages, context).

.. deprecated:: 0.2
    Bridges to the new AgentLoop API using an ephemeral project rooted
    at cwd. For new code prefer:
        from mini_cc import ProjectManager, SessionManager
    Scheduled for removal in 0.3 (M3-6 retirement plan).
"""
from __future__ import annotations

import warnings
from pathlib import Path

from .core.loop import AgentLoop, ProjectRef
from .mcp import MCPPool
from .sandbox import SubprocessSandbox
from .scheduler import CronScheduler
from .skills import SkillLoader
from .storage import FSStorage
from .teams import TeammateSpawner
from .tools.background import BackgroundScheduler


def agent_loop(messages: list, context: dict):
    """Run the new agent loop with cwd as the project workspace.

    Mutates `messages` in place (s20 semantics). Prints events to stdout.
    The caller is expected to have already appended the user turn to
    `messages` before calling, matching s20's __main__ block.
    """
    warnings.warn(
        "mini_cc._shim.agent_loop is deprecated and will be removed in "
        "0.3; use ProjectManager + SessionManager instead",
        DeprecationWarning,
        stacklevel=2,
    )
    cwd = Path.cwd()
    sandbox = SubprocessSandbox("shim", cwd)
    storage = FSStorage(cwd / ".mini_cc_state")
    skills_loader = SkillLoader(cwd)
    scheduler = CronScheduler("shim", storage)
    mcp_pool = MCPPool("shim")
    background = BackgroundScheduler()
    ref = ProjectRef(
        project_id="shim",
        project_root=str(cwd),
        sandbox=sandbox,
        storage=storage,
        skills_catalog=skills_loader.catalog(),
        skills_loader=skills_loader,
        scheduler=scheduler,
        mcp_pool=mcp_pool,
        background=background,
        mcp_servers=mcp_pool.list_connected(),
    )
    teams = TeammateSpawner(
        workspace=cwd,
        loop_factory=lambda sid: AgentLoop(ref, sid),
        project_id="shim",
        storage=storage,
    )
    ref.teams = teams
    loop = AgentLoop(ref, "shim")
    loop.messages = messages
    for ev in loop.run(None):
        # The user turn was already appended by the caller; pass None so
        # the loop doesn't append another one.
        t = ev.get("type")
        if t == "text":
            print(ev["text"])
        elif t == "tool_use":
            print(f"> {ev['name']}")
        elif t == "tool_result":
            print(str(ev.get("content"))[:300])
        elif t == "error":
            print(f"[error] {ev.get('message')}")
    return None
