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
    workflows/<wf_id>.json
    workflow_defs/<def_id>.json
    workflow_runs/<run_id>.json
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import CronJob, SessionMeta, Storage, Task


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


logger = logging.getLogger(__name__)


class StorageCorruptionError(RuntimeError):
    """Raised when a storage file exists but cannot be parsed.

    Distinguishes "session has no messages yet" (returns []) from
    "session file is truncated / half-written" (raises). Without this
    distinction a corrupted file looks identical to an empty session,
    so the next save silently overwrites it with a fresh single-message
    transcript — the user loses the prior conversation with no signal.
    """


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
        (p / "workflows").mkdir(parents=True, exist_ok=True)
        (p / "workflow_defs").mkdir(parents=True, exist_ok=True)
        (p / "workflow_runs").mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _safe_session(session_id: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)

    def load_messages(self, project_id, session_id):
        fp = self._proj(project_id) / "messages" / f"{self._safe_session(session_id)}.json"
        if not fp.exists():
            return []
        with self._lock(f"{project_id}:msg:{session_id}"):
            raw = fp.read_text(encoding="utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error(
                    "storage.corruption project=%s session=%s file=%s error=%s",
                    project_id, session_id, fp, e)
                raise StorageCorruptionError(
                    f"messages file for session {session_id!r} is corrupted: "
                    f"{type(e).__name__}: {e}. File: {fp}") from e

    def save_messages(self, project_id, session_id, msgs):
        fp = self._proj(project_id) / "messages" / f"{self._safe_session(session_id)}.json"
        with self._lock(f"{project_id}:msg:{session_id}"):
            self._atomic_write_json(
                fp, json.loads(json.dumps(msgs, ensure_ascii=False, default=str)))
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

    def search_messages(self, project_id, query, limit=20):
        """Substring search across every session's messages in a project.

        Returns top-N SearchHit objects (session_id + role + windowed
        snippet + message_index). Case-insensitive. Implementation is
        deliberately simple linear scan — adequate for moderate scale;
        swap to sqlite FTS5 later without changing the call site.
        """
        from .base import SearchHit
        q = (query or "").lower()
        if not q.strip():
            return []
        proj_dir = self._proj(project_id)
        msgs_dir = proj_dir / "messages"
        if not msgs_dir.exists():
            return []
        hits: list[SearchHit] = []
        WINDOW = 80  # chars of context on each side of the match
        try:
            files = sorted(msgs_dir.glob("*.json"))
        except OSError:
            return []
        for fp in files:
            if len(hits) >= limit:
                break
            session_id = fp.stem
            try:
                msgs = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for i, m in enumerate(msgs):
                if len(hits) >= limit:
                    break
                role = m.get("role", "")
                text = _extract_text(m.get("content"))
                if not text:
                    continue
                low = text.lower()
                pos = low.find(q)
                if pos < 0:
                    continue
                start = max(0, pos - WINDOW)
                end = min(len(text), pos + len(q) + WINDOW)
                snippet = text[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(text):
                    snippet = snippet + "…"
                hits.append(SearchHit(session_id=session_id, role=role,
                                      snippet=snippet, message_index=i))
        return hits

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
        """Remove everything for a session: messages, todos, events log,
        and the sessions-index entry. Used by SessionManager.remove so
        deleted sessions don't resurface via the back-compat scan."""
        safe = self._safe_session(session_id)
        proj = self._proj(project_id)
        for fp in (proj / "messages" / f"{safe}.json",
                   proj / "todos" / f"{safe}.json",
                   proj / "sessions" / f"{safe}.events.jsonl"):
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
        except json.JSONDecodeError as e:
            logger.error(
                "storage.corruption project=%s session=%s file=%s error=%s",
                project_id, session_id, fp, e)
            raise StorageCorruptionError(
                f"todos file for session {session_id!r} is corrupted: "
                f"{type(e).__name__}: {e}. File: {fp}") from e

    def save_todos(self, project_id, session_id, todos):
        fp = self._proj(project_id) / "todos" / f"{self._safe_session(session_id)}.json"
        self._atomic_write_json(fp, todos)

    def load_tasks(self, project_id):
        d = self._proj(project_id) / "tasks"
        out = []
        for fp in d.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                out.append(Task(**raw))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(
                    "storage.corruption project=%s file=%s error=%s — skipping",
                    project_id, fp, e)
                continue
        return out

    def save_task(self, project_id, task):
        fp = self._proj(project_id) / "tasks" / f"{task.id}.json"
        self._atomic_write_json(fp, task.__dict__)

    def claim_task_atomic(self, project_id, task_id, owner: str) -> tuple[bool, str]:
        """Compare-and-swap task claim. Returns (ok, message).

        Atomically: load task → verify status=pending & owner=None →
        set owner + status=in_progress → save. The whole read-modify-
        write is serialized on a per-task lock, so two teammates
        idling in parallel can't both succeed against the same task.

        Without this, the load/save sequence in TeammateManager.
        _claim_task races: both see pending, both write owner=self,
        the second write wins silently.
        """
        with self._lock(f"{project_id}:task:{task_id}"):
            fp = self._proj(project_id) / "tasks" / f"{task_id}.json"
            if not fp.exists():
                return False, f"Error: task {task_id} not found"
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                logger.error(
                    "storage.corruption project=%s task=%s file=%s error=%s",
                    project_id, task_id, fp, e)
                return False, (
                    f"Error: task {task_id} file is corrupted: "
                    f"{type(e).__name__}")
            try:
                t = Task(**raw)
            except TypeError as e:
                return False, f"Error: task {task_id} schema: {e}"
            if t.status != "pending":
                return False, f"Task {task_id} is {t.status}, cannot claim"
            if t.owner:
                return False, f"Task {task_id} already owned by {t.owner}"
            t.owner = owner
            t.status = "in_progress"
            self._atomic_write_json(fp, t.__dict__)
            return True, f"Claimed {t.id} ({t.subject})"

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
        self._atomic_write_json(
            fp, [j.__dict__ for j in jobs])

    def write_transcript(self, project_id, msgs):
        fp = (self._proj(project_id) / "transcripts"
              / f"transcript_{int(time.time())}.jsonl")
        with fp.open("a", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")

    # ── Session event log (for SSE Last-Event-Id replay) ───────────────
    def _session_events_path(self, project_id, session_id) -> Path:
        return (self._proj(project_id) / "sessions"
                / f"{self._safe_session(session_id)}.events.jsonl")

    def append_session_event(self, project_id, session_id, event: dict) -> int:
        """Append one event to the session's event log, returning the
        assigned sequence id (1-based monotonic per session).

        The event is written as a single JSON line: ``{"seq": N, "payload": ...}``.
        Reads scan from the start, so this is adequate for sessions up to
        ~10k events; beyond that, swap to a sqlite-backed implementation
        without changing the call site.
        """
        fp = self._session_events_path(project_id, session_id)
        with self._lock(f"{project_id}:evt:{session_id}"):
            current = self._session_event_count_locked(fp)
            seq = current + 1
            with fp.open("a", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"seq": seq, "payload": event},
                    ensure_ascii=False, default=str) + "\n")
            return seq

    @staticmethod
    def _session_event_count_locked(fp: Path) -> int:
        if not fp.exists():
            return 0
        try:
            with fp.open("r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def read_session_events_since(self, project_id, session_id,
                                  last_seq: int) -> list[dict]:
        """Return payloads for all events with seq > last_seq, in order."""
        fp = self._session_events_path(project_id, session_id)
        if not fp.exists():
            return []
        out: list[dict] = []
        with self._lock(f"{project_id}:evt:{session_id}"):
            try:
                with fp.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("seq", 0) > last_seq:
                            out.append(rec.get("payload", {}))
            except OSError:
                return []
        return out

    def session_event_count(self, project_id, session_id) -> int:
        fp = self._session_events_path(project_id, session_id)
        with self._lock(f"{project_id}:evt:{session_id}"):
            return self._session_event_count_locked(fp)

    def write_tool_result(self, project_id, tool_use_id, text):
        fp = self._proj(project_id) / "tool_results" / f"{tool_use_id}.txt"
        fp.write_text(text, encoding="utf-8")

    def read_tool_result(self, project_id, tool_use_id):
        fp = self._proj(project_id) / "tool_results" / f"{tool_use_id}.txt"
        if not fp.exists():
            return None
        return fp.read_text(encoding="utf-8")

    # ── Workflows ──────────────────────────────────────────────────────
    @staticmethod
    def _safe_wf_id(wf_id: str) -> str:
        # Same character policy as session ids so workflow ids can't
        # escape the workflows/ subdir via ../.
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in wf_id)

    def save_workflow(self, project_id, wf_dict):
        wf_id = self._safe_wf_id(wf_dict.get("id") or "wf_unknown")
        with self._lock(f"{project_id}:wf:{wf_id}"):
            payload = dict(wf_dict)
            payload["id"] = wf_id
            payload.setdefault("saved_at", _iso_now())
            fp = self._proj(project_id) / "workflows" / f"{wf_id}.json"
            self._atomic_write_json(fp, payload)

    def load_workflow(self, project_id, wf_id) -> dict | None:
        fp = self._proj(project_id) / "workflows" / f"{self._safe_wf_id(wf_id)}.json"
        if not fp.exists():
            return None
        with self._lock(f"{project_id}:wf:{wf_id}"):
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None

    def list_workflows(self, project_id) -> list[dict]:
        d = self._proj(project_id) / "workflows"
        out: list[dict] = []
        for fp in d.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.append(raw)
            except (json.JSONDecodeError, OSError):
                continue
        out.sort(key=lambda w: w.get("saved_at", ""), reverse=True)
        return out

    def delete_workflow(self, project_id, wf_id) -> bool:
        fp = self._proj(project_id) / "workflows" / f"{self._safe_wf_id(wf_id)}.json"
        if not fp.exists():
            return False
        try:
            fp.unlink()
            return True
        except FileNotFoundError:
            return False

    # ── Workflow V2 — definitions + runs ────────────────────────────────
    # Separated from the legacy workflows/ bag: defs are versioned
    # templates, runs are execution instances. Kept in their own
    # subdirs so the legacy tools/workflow.py state isn't disturbed.

    @staticmethod
    def _safe_v2_id(id_str: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in id_str)

    def save_workflow_def(self, project_id, def_dict) -> None:
        def_id = self._safe_v2_id(def_dict.get("def_id") or "wfdef_unknown")
        with self._lock(f"{project_id}:wfdef:{def_id}"):
            fp = self._proj(project_id) / "workflow_defs" / f"{def_id}.json"
            self._atomic_write_json(fp, def_dict)

    def load_workflow_def(self, project_id, def_id) -> dict | None:
        fp = self._proj(project_id) / "workflow_defs" / f"{self._safe_v2_id(def_id)}.json"
        if not fp.exists():
            return None
        with self._lock(f"{project_id}:wfdef:{def_id}"):
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None

    def list_workflow_defs(self, project_id) -> list[dict]:
        d = self._proj(project_id) / "workflow_defs"
        out: list[dict] = []
        for fp in d.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.append(raw)
            except (json.JSONDecodeError, OSError):
                continue
        out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return out

    def delete_workflow_def(self, project_id, def_id) -> bool:
        fp = self._proj(project_id) / "workflow_defs" / f"{self._safe_v2_id(def_id)}.json"
        if not fp.exists():
            return False
        try:
            fp.unlink()
            return True
        except FileNotFoundError:
            return False

    def save_workflow_run(self, project_id, run_dict) -> None:
        run_id = self._safe_v2_id(run_dict.get("run_id") or "wfrun_unknown")
        with self._lock(f"{project_id}:wfrun:{run_id}"):
            fp = self._proj(project_id) / "workflow_runs" / f"{run_id}.json"
            self._atomic_write_json(fp, run_dict)

    def load_workflow_run(self, project_id, run_id) -> dict | None:
        fp = self._proj(project_id) / "workflow_runs" / f"{self._safe_v2_id(run_id)}.json"
        if not fp.exists():
            return None
        with self._lock(f"{project_id}:wfrun:{run_id}"):
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None

    def list_workflow_runs(self, project_id) -> list[dict]:
        d = self._proj(project_id) / "workflow_runs"
        out: list[dict] = []
        for fp in d.glob("*.json"):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.append(raw)
            except (json.JSONDecodeError, OSError):
                continue
        out.sort(key=lambda x: x.get("started_at") or "", reverse=True)
        return out


def _extract_text(content) -> str:
    """Flatten a message's `content` field to plain text.

    Anthropic-shaped: either a string ("hi") or a list of blocks:
    [{"type": "text", "text": "..."}, {"type": "tool_use", ...}, ...].
    Tool-use blocks contribute their name + input (so /search can hit
    "edit_file" commands); tool_result blocks contribute their content.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif t == "tool_use":
                parts.append(f"{b.get('name', '')} {json.dumps(b.get('input', {}), ensure_ascii=False)}")
            elif t == "tool_result":
                inner = b.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for ib in inner:
                        if isinstance(ib, dict) and ib.get("type") == "text":
                            parts.append(str(ib.get("text", "")))
        return " ".join(parts)
    return ""
