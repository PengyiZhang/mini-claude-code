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
                           project_guide: str = "",
                           vision_tools: Iterable | None = None) -> str:
    now = datetime.now()
    sections = [PROMPT_IDENTITY, PROMPT_TOOLS,
                f"Working directory: {project_root}",
                # Surface the date as its own line + explicit year so the
                # model doesn't fall back to its training-cutoff year in
                # time-sensitive tool calls (e.g. web search queries that
                # hard-code the year). debug.9.md: agent searched
                # "福田汽车股价 2025年7月" when today was 2026-07-03.
                (f"Today's date: {now.strftime('%Y-%m-%d')} "
                 f"({now.strftime('%A')}). Use this date for any "
                 f"'today', 'this week', 'current' reference; do NOT "
                 f"fall back to your training-cutoff year."),
                f"Current time: {now.isoformat(timespec='seconds')}"]
    if project_guide:
        sections.append("Project guide (authoritative — follow these conventions):\n"
                        + project_guide.strip())
    if skills_catalog:
        sections.append("Skills catalog:\n" + skills_catalog)
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    if mcp_servers:
        sections.append(f"Connected MCP servers: {', '.join(mcp_servers)}")
    vision_list = list(vision_tools) if vision_tools else []
    if vision_list:
        sections.append(_vision_section(vision_list))
    return "\n\n".join(sections)


def _vision_section(vision_tools: list) -> str:
    """Compose the vision-tools system-prompt section. Emitted only when
    the model can't process images natively — tells it which MCP tool to
    pass each signed URL to."""
    lines = ["This deployment's model does not process images directly. "
             "When a user message references an image at a URL "
             "(e.g. \"[image attached at http://.../shared/asset/sh_...]\"), "
             "call one of these vision tools with that URL:"]
    for t in vision_tools:
        name = getattr(t, "name", str(t))
        desc = (getattr(t, "description", "") or "").strip()
        if desc:
            lines.append(f"- {name}: {desc}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


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
