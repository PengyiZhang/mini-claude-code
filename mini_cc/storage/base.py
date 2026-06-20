"""Storage interface — pluggable persistence for multi-tenant agent state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


@dataclass
class CronJob:
    job_id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = False


class Storage(Protocol):
    """Per-project state persistence.

    All methods key on project_id; implementations must isolate storage
    between projects.
    """

    # Messages / todos
    def load_messages(self, project_id: str, session_id: str) -> list[dict]: ...
    def save_messages(self, project_id: str, session_id: str, msgs: list[dict]) -> None: ...
    def load_todos(self, project_id: str, session_id: str) -> list[dict]: ...
    def save_todos(self, project_id: str, session_id: str, todos: list[dict]) -> None: ...

    # Tasks
    def load_tasks(self, project_id: str) -> list[Task]: ...
    def save_task(self, project_id: str, task: Task) -> None: ...
    def delete_task(self, project_id: str, task_id: str) -> None: ...

    # Memory
    def load_memory(self, project_id: str) -> str: ...
    def append_memory(self, project_id: str, entry: str) -> None: ...

    # Cron
    def load_cron(self, project_id: str) -> list[CronJob]: ...
    def save_cron(self, project_id: str, jobs: list[CronJob]) -> None: ...

    # Transcripts
    def write_transcript(self, project_id: str, msgs: list[dict]) -> None: ...

    # Tool result cache (large tool outputs offloaded to disk)
    def write_tool_result(self, project_id: str, tool_use_id: str, text: str) -> None: ...
    def read_tool_result(self, project_id: str, tool_use_id: str) -> str | None: ...
