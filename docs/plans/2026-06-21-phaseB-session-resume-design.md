# Phase B: session resume across server restarts

## Context

Today `AgentLoop` already reloads messages and todos from disk at
construction (`core/loop.py:86-89`). What's missing is the layer
above it: `SessionManager` only knows about in-memory sessions, so
after a server restart `GET /sessions` returns `[]` and
`POST /sessions/{sid}/send` 404s even when the conversation is
safely on disk.

Phase B closes that gap. Goals:

1. List sessions from disk, with metadata (created / last-active /
   message count / in-memory flag).
2. Auto-resume on send (lazy warm-load).
3. Explicit resume endpoint for clients that want to prime a
   session before sending.
4. POST `/sessions` becomes idempotent: same body returns the same
   session (201 if new, 200 if re-warming).
5. Auto-repair transcripts that crashed mid-turn (assistant tool_use
   with no matching tool_result) by appending a synthetic
   tool_result.

## Approach (locked via brainstorm)

- **Resume trigger**: both — auto-resume on send AND explicit
  `POST /sessions/{sid}/resume`.
- **Metadata shape**: `SessionMeta{session_id, created_at,
  last_active_at, message_count, in_memory}`. Returned by
  `GET /sessions` (was `list[str]`).
- **Conflict policy**: POST `/sessions` is idempotent (200 if
  existing, 201 if new). Matches auto-resume semantics.
- **Crash repair**: append synthetic `tool_result` content
  `"[interrupted by server restart]"` for any dangling tool_use
  blocks at the tail of the transcript. Leave orphan user turns
  alone — the model just picks up from there.
- **Metadata storage**: single `<project>/sessions/index.json` per
  project. Back-compat scan rebuilds the index from
  `messages/*.json` file stats on first read.

## File-by-file

### `mini_cc/storage/base.py` (modify)

Add `SessionMeta` dataclass and three Storage methods:

```python
@dataclass
class SessionMeta:
    session_id: str
    created_at: str
    last_active_at: str
    message_count: int
    in_memory: bool = False  # filled in by SessionManager, not persisted

class Storage(Protocol):
    ...
    def list_sessions(self, project_id: str) -> list[SessionMeta]: ...
    def save_session_meta(self, project_id: str, meta: SessionMeta) -> None: ...
    def delete_session_meta(self, project_id: str, session_id: str) -> None: ...
```

### `mini_cc/storage/fs.py` (modify)

- Add `_sessions_dir(project_id)` (creates `<project>/sessions/`).
- `_load_sessions_index(project_id) -> dict[str, dict]` reads
  `index.json` under a per-file lock; if missing, scans
  `messages/*.json`, builds the index from file stat + content,
  persists. Back-compat upgrade path.
- `save_messages(project_id, session_id, msgs)` — after writing,
  upsert SessionMeta (bump message_count + last_active_at; seed
  created_at on first sighting). Reuses the existing per-file lock.
- `list_sessions(project_id)` — returns `[SessionMeta(**v) for v in
  index.values()]`.
- `save_session_meta` / `delete_session_meta` — direct mutations of
  the index with atomic rename-on-write.

### `mini_cc/core/loop.py` (modify)

Add module-level helper (no class change):

```python
def repair_dangling_tool_uses(messages: list[dict]) -> bool:
    """If the tail of the transcript is an assistant message with
    tool_use blocks that have no matching tool_result, append a
    synthetic user turn marking each as
    '[interrupted by server restart]'. Returns True if repair
    happened, False otherwise."""
```

Walks from the end: if last message is assistant with tool_use
blocks, find each tool_use.id, check the next user message has a
tool_result for it; if not, append a synthetic user turn.

### `mini_cc/session/manager.py` (modify)

- `Session` dataclass unchanged.
- `SessionManager.list()` returns `list[SessionMeta]` from storage,
  stamps `in_memory` from current `_sessions` dict.
- `_ensure_warm(project_id, session_id) -> Session`:
  - In memory → return existing.
  - On disk → `_warm`.
  - Neither → `KeyError`.
- `_warm(project_id, session_id) -> Session`: construct AgentLoop,
  call `repair_dangling_tool_uses(loop.messages)` in-place, persist
  if changed, stash in `_sessions`.
- `start_session(project_id, session_id=None, ...) -> Session`:
  - If `session_id` is given and exists on disk → `_warm` (idempotent
    resume path).
  - Else → allocate (or use given) session_id, write empty
    `messages` file to disk so SessionMeta is seeded, build loop.

### `mini_cc/server/schemas.py` (modify)

Add `SessionMeta` pydantic model. `SessionOut` unchanged.

### `mini_cc/server/routes/sessions.py` (modify)

- `GET /sessions` → returns `list[SessionMeta]`.
- `POST /sessions` → 201 on new, 200 on idempotent resume. Response
  body unchanged (`SessionOut`).
- `POST /sessions/{sid}/resume` → **new**. Returns `SessionMeta`.
  404 if not on disk. 200 if already warm.
- `DELETE /sessions/{sid}` → unchanged behavior, but also removes
  SessionMeta via `storage.delete_session_meta`.
- `POST /sessions/{sid}/send` → swap `sm.get` for `sm._ensure_warm`
  so cold sessions auto-resume. (`_ensure_warm` may raise KeyError →
  still 404.)

### `tests/test_phaseB_storage.py` (new)

- list_sessions empty for new project.
- save_messages seeds + bumps SessionMeta.
- save_session_meta / delete_session_meta round-trip.
- Back-compat: pre-seed `messages/s1.json` + `messages/s2.json`
  without index → list_sessions rebuilds index from file stat +
  persists.

### `tests/test_phaseB_resume.py` (new)

- SessionManager.list() in_memory flag flips correctly.
- _ensure_warm cold-load returns same instance on second call.
- start_session with existing id → idempotent resume.
- repair_dangling_tool_uses:
  - No-op on a clean transcript.
  - Appends synthetic tool_result when tail has dangling tool_use.
  - Doesn't touch mid-transcript tool_use that already has results.

### `tests/test_phaseB_http.py` (new)

- POST /sessions twice with same id → 201 then 200.
- GET /sessions returns SessionMeta list with in_memory.
- POST /sessions/{sid}/resume on cold → 200 + warms.
- POST /sessions/{sid}/resume on unknown → 404.
- "Restart" simulation: two SessionManagers, same data_dir; second
  sees the first's session, send auto-resumes.

### `mini_cc/README.md` + `.zh.md` (modify)

- Remove "Session resume across server restarts" from Out-of-scope.
- Add `POST /sessions/{sid}/resume` row to endpoint table.
- Document SessionMeta schema + cold/warm semantics under a new
  "Sessions" subsection.

## Verification

```bash
python -m pytest tests/test_phaseB_storage.py -v
python -m pytest tests/test_phaseB_resume.py -v
python -m pytest tests/test_phaseB_http.py -v
python -m pytest tests/ -q   # expect 240+ passing
```

Manual:

```bash
# Start server, create a project + session, send a few turns
python -m mini_cc.server keygen t1
python -m mini_cc.server
# (in another shell) POST project, POST session, POST send 3x

# Kill the server (Ctrl-C). Restart it. The session is still there:
curl .../sessions        # → lists it
curl .../sessions/s1/send -d '{"user_input":"hi again"}'  # auto-resumes
```

## Out of scope for Phase B

- Multi-instance horizontal resume (sessions pinned to one node).
  Today single-process; revisit if we add a session affinity layer.
- Undo of mid-turn-crash repair (no "discard synthetic result"
  path). The synthetic tool_result is indistinguishable from a real
  one to the model.
- Reconnecting to a still-running SSE stream after a network blip
  (client-side concern; SSE sentinel + new send works fine).
