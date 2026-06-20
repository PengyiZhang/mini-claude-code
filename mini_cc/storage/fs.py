"""Filesystem-backed Storage implementation.

Layout under <state_root>/<project_id>/:
    messages/<session_id>.json
    todos/<session_id>.json
    tasks/<task_id>.json
    memory/MEMORY.md
    cron/jobs.json
    transcripts/transcript_<ts>.jsonl
    tool_results/<tool_use_id>.txt
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .base import CronJob, Storage, Task


class FSStorage:
    """Default Storage implementation. One root directory, projects isolated
    by subdirectory."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _proj(self, project_id: str) -> Path:
        p = self.root / project_id
        (p / "messages").mkdir(parents=True, exist_ok=True)
        (p / "todos").mkdir(parents=True, exist_ok=True)
        (p / "tasks").mkdir(parents=True, exist_ok=True)
        (p / "memory").mkdir(parents=True, exist_ok=True)
        (p / "cron").mkdir(parents=True, exist_ok=True)
        (p / "transcripts").mkdir(parents=True, exist_ok=True)
        (p / "tool_results").mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _safe_session(session_id: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)

    def load_messages(self, project_id, session_id):
        fp = self._proj(project_id) / "messages" / f"{self._safe_session(session_id)}.json"
        if not fp.exists():
            return []
        with self._lock(f"{project_id}:msg:{session_id}"):
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []

    def save_messages(self, project_id, session_id, msgs):
        fp = self._proj(project_id) / "messages" / f"{self._safe_session(session_id)}.json"
        with self._lock(f"{project_id}:msg:{session_id}"):
            fp.write_text(json.dumps(msgs, ensure_ascii=False, default=str),
                          encoding="utf-8")

    def load_todos(self, project_id, session_id):
        fp = self._proj(project_id) / "todos" / f"{self._safe_session(session_id)}.json"
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def save_todos(self, project_id, session_id, todos):
        fp = self._proj(project_id) / "todos" / f"{self._safe_session(session_id)}.json"
        fp.write_text(json.dumps(todos, ensure_ascii=False), encoding="utf-8")

    def load_tasks(self, project_id):
        d = self._proj(project_id) / "tasks"
        out = []
        for fp in d.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                out.append(Task(**raw))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def save_task(self, project_id, task):
        fp = self._proj(project_id) / "tasks" / f"{task.id}.json"
        fp.write_text(json.dumps(task.__dict__, ensure_ascii=False), encoding="utf-8")

    def delete_task(self, project_id, task_id):
        fp = self._proj(project_id) / "tasks" / f"{task_id}.json"
        if fp.exists():
            fp.unlink()

    def load_memory(self, project_id):
        fp = self._proj(project_id) / "memory" / "MEMORY.md"
        if not fp.exists():
            return ""
        return fp.read_text(encoding="utf-8")

    def append_memory(self, project_id, entry):
        fp = self._proj(project_id) / "memory" / "MEMORY.md"
        with self._lock(f"{project_id}:memory"):
            with fp.open("a", encoding="utf-8") as f:
                f.write(entry.rstrip() + "\n")

    def load_cron(self, project_id):
        fp = self._proj(project_id) / "cron" / "jobs.json"
        if not fp.exists():
            return []
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            return [CronJob(**j) for j in raw]
        except (json.JSONDecodeError, TypeError):
            return []

    def save_cron(self, project_id, jobs):
        fp = self._proj(project_id) / "cron" / "jobs.json"
        fp.write_text(
            json.dumps([j.__dict__ for j in jobs], ensure_ascii=False, indent=2),
            encoding="utf-8")

    def write_transcript(self, project_id, msgs):
        fp = (self._proj(project_id) / "transcripts"
              / f"transcript_{int(time.time())}.jsonl")
        with fp.open("a", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")

    def write_tool_result(self, project_id, tool_use_id, text):
        fp = self._proj(project_id) / "tool_results" / f"{tool_use_id}.txt"
        fp.write_text(text, encoding="utf-8")

    def read_tool_result(self, project_id, tool_use_id):
        fp = self._proj(project_id) / "tool_results" / f"{tool_use_id}.txt"
        if not fp.exists():
            return None
        return fp.read_text(encoding="utf-8")
