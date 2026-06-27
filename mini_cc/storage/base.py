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


@dataclass
class SessionMeta:
    """Per-session metadata for resume across server restarts.

    `in_memory` is filled in at runtime by the SessionManager and is
    not persisted to the index file.
    """
    session_id: str
    created_at: str
    last_active_at: str
    message_count: int
    in_memory: bool = False


@dataclass
class SearchHit:
    """One matching fragment from a /search query (F3.1)."""
    session_id: str
    role: str          # "user" | "assistant" | "system"
    snippet: str       # windowed text around the match
    message_index: int  # 0-based position in the session's messages


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

    # Session index (resume-across-restart)
    def list_sessions(self, project_id: str) -> list[SessionMeta]: ...
    def save_session_meta(self, project_id: str, meta: SessionMeta) -> None: ...
    def delete_session_meta(self, project_id: str, session_id: str) -> None: ...
    def delete_session(self, project_id: str, session_id: str) -> None: ...

    # Cross-session search (F3.1)
    def search_messages(self, project_id: str, query: str,
                        limit: int = 20) -> list["SearchHit"]: ...

    # Transcripts
    def write_transcript(self, project_id: str, msgs: list[dict]) -> None: ...

    # Tool result cache (large tool outputs offloaded to disk)
    def write_tool_result(self, project_id: str, tool_use_id: str, text: str) -> None: ...
    def read_tool_result(self, project_id: str, tool_use_id: str) -> str | None: ...

    # Workflows (saved definitions + accumulated state, keyed by workflow id)
    def save_workflow(self, project_id: str, wf_dict: dict) -> None: ...
    def load_workflow(self, project_id: str, wf_id: str) -> dict | None: ...
    def list_workflows(self, project_id: str) -> list[dict]: ...
    def delete_workflow(self, project_id: str, wf_id: str) -> bool: ...
