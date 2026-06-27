"""System prompt assembly — per-project, parameterized."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable


PROMPT_IDENTITY = "You are a coding agent. Act, don't explain."

PROMPT_TOOLS = ("Available tools: bash, read_file, write_file, edit_file, "
                "glob, grep, todo_write. More tools (skills, mcp, cron, "
                "teams, worktrees) are being ported incrementally.")

PROMPT_MEMORY = "Relevant memories are injected below when available."


def assemble_system_prompt(*,
                           project_root: Path,
                           tools: Iterable,
                           memories: str = "",
                           mcp_servers: list[str] | None = None,
                           skills_catalog: str = "",
                           project_guide: str = "") -> str:
    sections = [PROMPT_IDENTITY, PROMPT_TOOLS,
                f"Working directory: {project_root}",
                f"Current time: {datetime.now().isoformat(timespec='seconds')}"]
    if project_guide:
        sections.append("Project guide (authoritative — follow these conventions):\n"
                        + project_guide.strip())
    if skills_catalog:
        sections.append("Skills catalog:\n" + skills_catalog)
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    if mcp_servers:
        sections.append(f"Connected MCP servers: {', '.join(mcp_servers)}")
    return "\n\n".join(sections)


def load_project_guide(project_root: Path) -> str:
    """Read <project_root>/.mini_cc/PROJECT.md if present; empty string otherwise.

    The guide is a CLAUDE.md-style authoritative project doc — conventions,
    architecture notes, do/don't lists. Injected into every turn's system
    prompt so the agent follows project rules without re-prompting.
    """
    fp = Path(project_root) / ".mini_cc" / "PROJECT.md"
    if not fp.is_file():
        return ""
    try:
        return fp.read_text(encoding="utf-8")
    except OSError:
        return ""
