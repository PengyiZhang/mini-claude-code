"""mini_cc — a backend-integrable mini Claude Code agent framework.

Multi-tenant: each project is identified by a unique project_id and isolated
to a workspace directory. State is pluggable (default filesystem).

Quick start:
    from mini_cc import ProjectManager, SessionManager
    pm = ProjectManager("/data/projects")
    pm.create(tenant_id="t1", project_id="proj-a")
    sm = SessionManager(pm)
    sess = sm.start_session("proj-a")
    for ev in sm.send("proj-a", sess.session_id, "hello"):
        print(ev)
"""
from __future__ import annotations

from .config import AnthropicConfig, default_config, set_default_config
from .core import (AgentLoop, DENY_LIST, DESTRUCTIVE, Hooks, ProjectRef,
                   RecoveryState, assemble_system_prompt, make_permission_hook,
                   spawn_subagent)
from .mcp import MCPClient, MCPPool, normalize_mcp_name
from .projects import Project, ProjectManager, ProjectMeta
from .sandbox import (CommandBlockedError, PathEscapeError, Policy,
                      Sandbox, SubprocessSandbox, Violation)
from .scheduler import CronScheduler
from .session import Session, SessionManager
from .skills import Skill, SkillLoader
from .storage import CronJob, FSStorage, Storage, Task
from .teams import (MessageBus, ProtocolState, ProtocolTracker, TeammateInfo,
                    TeammateSpawner)
from .tools import FunctionTool, Tool, ToolContext, builtin_tools
from .tools.background import BackgroundScheduler

__version__ = "0.1.0"

__all__ = [
    # config
    "AnthropicConfig", "default_config", "set_default_config",
    # core
    "AgentLoop", "ProjectRef", "RecoveryState", "assemble_system_prompt",
    "Hooks", "make_permission_hook", "DENY_LIST", "DESTRUCTIVE",
    "spawn_subagent",
    # projects
    "Project", "ProjectManager", "ProjectMeta",
    # sandbox
    "Sandbox", "SubprocessSandbox", "Policy", "Violation",
    "PathEscapeError", "CommandBlockedError",
    # session
    "Session", "SessionManager",
    # storage
    "Storage", "FSStorage", "Task", "CronJob",
    # skills
    "Skill", "SkillLoader",
    # scheduler
    "CronScheduler",
    # teams
    "MessageBus", "ProtocolState", "ProtocolTracker",
    "TeammateInfo", "TeammateSpawner",
    # mcp
    "MCPClient", "MCPPool", "normalize_mcp_name",
    # tools
    "Tool", "FunctionTool", "ToolContext", "builtin_tools",
    "BackgroundScheduler",
]
