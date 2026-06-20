"""Backward-compat shim: expose s20-style agent_loop(messages, context).

Bridges to the new AgentLoop API using an ephemeral project rooted at cwd.
For new code prefer:
    from mini_cc import ProjectManager, SessionManager
"""
from __future__ import annotations

from pathlib import Path

from .core.loop import AgentLoop, ProjectRef
from .sandbox import SubprocessSandbox
from .storage import FSStorage


def agent_loop(messages: list, context: dict):
    """Run the new agent loop with cwd as the project workspace.

    Mutates `messages` in place (s20 semantics). Prints events to stdout.

    The caller is expected to have already appended the user turn to
    `messages` before calling, matching s20's __main__ block.
    """
    cwd = Path.cwd()
    sandbox = SubprocessSandbox("shim", cwd)
    storage = FSStorage(cwd / ".mini_cc_state")
    ref = ProjectRef(
        project_id="shim",
        project_root=str(cwd),
        sandbox=sandbox,
        storage=storage,
    )
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
