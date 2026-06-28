"""mini_cc — a backend-integrable mini Claude Code agent framework.

Multi-tenant: each project is identified by a unique project_id and isolated
to a workspace directory. State is pluggable (default filesystem).

Quick start:
    import os
    from mini_cc import (AnthropicConfig, ProjectManager, SessionManager,
                         set_default_config)

    # 1. Configure LLM credentials BEFORE the first send. The default
    #    config reads from env at first use, but explicit is safer — and
    #    required if you're routing to a non-default provider.
    set_default_config(AnthropicConfig(
        api_key=os.environ["ANTHROPIC_API_KEY"],     # or LITELLM_API_KEY
        # primary_model="openai/gpt-4",              # litellm-routed models
        # litellm_api_key=os.environ["LITELLM_API_KEY"],
    ))

    # 2. Stand up a project (one per tenant workspace).
    pm = ProjectManager("/data/projects")
    pm.create(tenant_id="t1", project_id="proj-a")

    # 3. Start a session and stream events.
    sm = SessionManager(pm)
    sess = sm.start_session("proj-a")
    for ev in sm.send("proj-a", sess.session_id, "hello"):
        print(ev)

Without step 1, the first send raises a 401 from the LLM provider; the
SDK has no implicit credential discovery. ``AnthropicConfig.has_llm_credentials``
returns False in that state — useful for preflight checks.
"""
from __future__ import annotations

from .config import AnthropicConfig, default_config, set_default_config
from .auth import TenantKeyRegistry, TenantPrincipal
from .core import (AgentLoop, DENY_LIST, DESTRUCTIVE, Hooks, ProjectRef,
                   RecoveryState, assemble_system_prompt, make_audit_hook,
                   make_large_output_hook, make_log_hook,
                   make_permission_hook, spawn_subagent)
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
    # auth
    "TenantKeyRegistry", "TenantPrincipal",
    # core
    "AgentLoop", "ProjectRef", "RecoveryState", "assemble_system_prompt",
    "Hooks", "make_permission_hook", "make_log_hook",
    "make_large_output_hook", "make_audit_hook",
    "DENY_LIST", "DESTRUCTIVE",
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
