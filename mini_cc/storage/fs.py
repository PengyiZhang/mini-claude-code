"""Filesystem-backed Storage implementation.

Layout under <state_root>/<project_id>/:
    messages/<session_id>.json
    todos/<session_id>.json
    tasks/<task_id>.json
    memory/MEMORY.md
    cron/jobs.json
    sessions/index.json
    transcripts/transcript_<ts>.jsonl
    tool_results/<tool_use_id>.txt
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import CronJob, SessionMeta, Storage, Task


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
        (p / "sessions").mkdir(parents=True, exist_ok=True)
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
        # Keep sessions/index.json in sync so list_sessions is accurate
        # without scanning the messages dir on every call.
        self._upsert_session_meta_locked(project_id, session_id, len(msgs))

    # ── Sessions index ─────────────────────────────────────────────────
    def _sessions_index_path(self, project_id: str) -> Path:
        return self._proj(project_id) / "sessions" / "index.json"

    def _load_sessions_index(self, project_id: str) -> dict[str, dict]:
        """Load the per-project sessions index, rebuilding it from the
        messages/ dir on first access (back-compat for projects that
        predate the index file)."""
        fp = self._sessions_index_path(project_id)
        with self._lock(f"{project_id}:sessions"):
            if fp.exists():
                try:
                    return json.loads(fp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass  # fall through to rebuild
            # Back-compat scan: derive index from existing message files.
            msgs_dir = self._proj(project_id) / "messages"
            index: dict[str, dict] = {}
            for mfp in msgs_dir.glob("*.json"):
                sid = mfp.stem
                try:
                    content = json.loads(mfp.read_text(encoding="utf-8"))
                    count = len(content) if isinstance(content, list) else 0
                except (json.JSONDecodeError, OSError):
                    count = 0
                st = mfp.stat()
                iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")
                index[sid] = {
                    "session_id": sid,
                    "created_at": iso,
                    "last_active_at": iso,
                    "message_count": count,
                }
            self._atomic_write_json(fp, index)
            return index

    @staticmethod
    def _atomic_write_json(fp: Path, payload) -> None:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json",
                                   dir=str(fp.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, fp)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _upsert_session_meta_locked(self, project_id, session_id, count) -> None:
        """Upsert one session's metadata. Caller-side I/O; takes the
        sessions lock internally."""
        with self._lock(f"{project_id}:sessions"):
            fp = self._sessions_index_path(project_id)
            try:
                index = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
            except json.JSONDecodeError:
                index = {}
            now = _iso_now()
            entry = index.get(session_id)
            if entry is None:
                index[session_id] = {
                    "session_id": session_id,
                    "created_at": now,
                    "last_active_at": now,
                    "message_count": count,
                }
            else:
                entry["last_active_at"] = now
                entry["message_count"] = count
                index[session_id] = entry
            self._atomic_write_json(fp, index)

    def list_sessions(self, project_id) -> list[SessionMeta]:
        index = self._load_sessions_index(project_id)
        return [SessionMeta(**v) for v in index.values()]

    def save_session_meta(self, project_id, meta: SessionMeta) -> None:
        with self._lock(f"{project_id}:sessions"):
            fp = self._sessions_index_path(project_id)
            try:
                index = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
            except json.JSONDecodeError:
                index = {}
            d = {k: v for k, v in meta.__dict__.items() if k != "in_memory"}
            index[meta.session_id] = d
            self._atomic_write_json(fp, index)

    def delete_session_meta(self, project_id, session_id) -> None:
        with self._lock(f"{project_id}:sessions"):
            fp = self._sessions_index_path(project_id)
            if not fp.exists():
                return
            try:
                index = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return
            if session_id in index:
                del index[session_id]
                self._atomic_write_json(fp, index)

    def delete_session(self, project_id, session_id) -> None:
        """Remove everything for a session: messages, todos, and the
        sessions-index entry. Used by SessionManager.remove so deleted
        sessions don't resurface via the back-compat scan."""
        safe = self._safe_session(session_id)
        proj = self._proj(project_id)
        for fp in (proj / "messages" / f"{safe}.json",
                   proj / "todos" / f"{safe}.json"):
            try:
                fp.unlink()
            except FileNotFoundError:
                pass
        self.delete_session_meta(project_id, session_id)

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
