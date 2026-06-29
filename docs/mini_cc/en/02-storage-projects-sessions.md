[ < [01](01-overview.md) ] [ **03** > ] · [中文版本](../zh/02-storage-projects-sessions.md)

# 02 — Storage, Projects, Sessions

> Three things that the conceptual chapters usually gloss over: **where
> state lives on disk**, **what a project is composed of**, and **how one
> turn of conversation gets serialized**. This chapter takes them down to
> the metal and flags production pitfalls: atomic writes, the sessions
> index, per-project locks, and cold/warm session resume.

---

## Problem & motivation

Once you move Claude Code from a single-machine script to a multi-tenant
backend, state management is the first thing that breaks:

- **Crash recovery**: the server gets OOM-killed mid-turn. How does the
  next boot resume the conversation? Anthropic's messages API requires
  every `tool_use` block to be followed by its `tool_result`, otherwise
  the next stream 4xxs. If the last on-disk message is an orphaned
  `tool_use`, the model is stuck.
- **Concurrency correctness**: two HTTP requests send to the same project
  at once. LLM calls are stateful; the messages file gets clobbered. But
  different projects must run fully in parallel — a single global lock
  won't do.
- **Physical guarantee of tenant isolation**: two tenants using the same
  `project_id` must not share a workspace or a storage subdir, or messages
  leak across tenants.
- **An observable session list**: the Web UI needs to show "which sessions
  are on disk, which are in memory" — without scanning the `messages/`
  dir every time (too slow at thousands of sessions).
- **Cross-session search**: a user asks "where did I say deploy before?"
  — plain substring scan is enough, no need for sqlite FTS, but the
  interface should leave room to swap implementations.

mini_cc's answer is three interlocking abstractions: the `Storage` Protocol
(swappable backend), `ProjectManager` (assembly + cache + layout), and
`SessionManager` (cold/warm sessions + per-project serialization). None
depends on the HTTP layer — the SDK uses the same semantics in-process.

---

## Design & principles

### 1. Storage Protocol — swappable backend

`mini_cc/storage/base.py:51` defines a Protocol; every method keys on
`project_id` and implementations must isolate by project:

```python
# mini_cc/storage/base.py:51
class Storage(Protocol):
    def load_messages(self, project_id, session_id) -> list[dict]: ...
    def save_messages(self, project_id, session_id, msgs) -> None: ...
    def list_sessions(self, project_id) -> list[SessionMeta]: ...
    def save_session_meta(self, project_id, meta: SessionMeta) -> None: ...
    def search_messages(self, project_id, query, limit=20) -> list["SearchHit"]: ...
    def write_transcript(self, project_id, msgs) -> None: ...
    # also todos / tasks / cron / memory / workflows / tool_results / events
```

The default implementation `FSStorage` (`mini_cc/storage/fs.py:48`) lays
each project's state under `<state_root>/<project_id>/`:

```
<state_root>/<project_id>/
  messages/<session_id>.json       ← all messages for one session
  todos/<session_id>.json
  tasks/<task_id>.json
  memory/MEMORY.md
  cron/jobs.json
  sessions/index.json              ← SessionMeta index (avoids scanning messages/)
  sessions/<session_id>.events.jsonl  ← SSE event stream (for Last-Event-Id replay)
  transcripts/transcript_<ts>.jsonl
  tool_results/<tool_use_id>.txt   ← large outputs offloaded; context keeps a ref
  workflows/<wf_id>.json
  workflow_defs/<def_id>.json      ← Workflow V2: definitions + runs separated
  workflow_runs/<run_id>.json
```

Three details worth remembering:

- **Atomic writes.** All JSON writes go through `_atomic_write_json`
  (`mini_cc/storage/fs.py:146`): write to a `tempfile.mkstemp` temp file,
  then `os.replace` the target. `os.replace` is atomic on both POSIX and
  Windows, so a reader always sees a complete old or new version — never
  a half-written JSON.
- **Explicit corruption errors.** `load_messages` raises
  `StorageCorruptionError` (`storage/fs.py:37`) on parse failure rather
  than returning `[]`. The reason is in the docstring: a corrupted file
  mistaken for "empty session" gets overwritten by the next save with a
  single-message transcript — the user loses the prior conversation with
  no signal.
- **Cross-session search.** `search_messages` (`storage/fs.py:189`) is a
  plain linear scan with an 80-char windowed snippet. The comment says
  "adequate for moderate scale; swap to sqlite FTS5 later without changing
  the call site" — the swap point is reserved.

### 2. ProjectManager — assembly + cache + layout

`ProjectManager` (`mini_cc/projects/manager.py:123`) is responsible for:

- **Creating / listing / deleting projects**, with ID validation
  `[A-Za-z0-9_-]+` (`_validate_project_id`, `manager.py:44`). This closes
  the path-traversal hole in `delete`'s `shutil.rmtree`.
- **Assembly** (`_assemble`, `manager.py:278`): wires every dependency a
  project needs (sandbox, storage, skills, scheduler, mcp, teams, hooks,
  permissions) in one shot.
- **Caching the assembled result** (`get`, `manager.py:225`), invalidated
  by config-file mtime. This matters because every API route calls
  `pm.get()` per request; without caching, re-assembly would re-connect
  remote MCP servers (~2s) and leak subprocesses.

The cache invalidation rule lives in `_config_signature`
(`manager.py:152`):

```python
# mini_cc/projects/manager.py:152
def _config_signature(self, tenant_id, workspace):
    """Stat .mcp.json / mcp.toml / permissions.toml in each tier dir,
    pack (path, mtime_ns, size) into a tuple. An mtime bump invalidates."""
```

The tenant-isolated directory layout is in `mini_cc/projects/layout.py:1`:

```
<data>/tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}
<data>/tenants/<tid>/.storage/<pid>/       ← tenant storage root
```

**`create()` deliberately does NOT prime the cache** (see `manager.py:215`
comment). Reason: `create()` returns before the caller has finished
configuring the workspace (e.g. writing `.mini_cc/permissions.toml`).
Priming now would pin a half-configured Project into the cache; a later
`get()` would return that stale object. The first `get()` lazily
assembles — after the workspace is fully set up — and caches that.

### 3. SessionManager — cold/warm sessions + per-project lock

`SessionManager` (`mini_cc/session/manager.py:51`) bridges the SDK core
and HTTP. It holds the dict of warm `AgentLoop` instances keyed by
`(project_id, session_id)`.

**Cold vs warm**: a session is **cold** when it exists on disk but no
`AgentLoop` is built for it in the current process, and **warm** once an
`AgentLoop` holds its transcript in memory. Three resume paths:

```
                  POST /sessions/{sid}/send
                            │
                            ▼
                  _ensure_warm(project_id, sid)
                            │
                ┌───────────┴────────────┐
                │ in self._sessions?     │
                └───────┬────────────────┘
                  no    │      yes → return warm session directly
                        ▼
                  storage.list_sessions(project_id) contains sid?
                  no → KeyError → 404 at the route layer
                        │ yes
                        ▼
                  _warm(project, sid):
                    AgentLoop(ref, sid) rebuilt
                    repair_dangling_tool_uses(messages)  ← crash recovery
                    save_messages if repaired
```

**The crash-recovery crux**: `repair_dangling_tool_uses` (`core/loop.py`)
checks whether the transcript's tail has an assistant message with
`tool_use` blocks but no matching `tool_result`. If so, it appends a
synthetic user turn with one `tool_result` per dangling id, content
`[interrupted by server restart]`, `is_error: true`. This preserves
context and lets the model continue — Anthropic's messages API won't 4xx
over a missing tool_result.

**The per-project serialization lock** (`session/manager.py:183`):

```python
# mini_cc/session/manager.py:183
def send(self, project_id, session_id, user_input):
    sess = self._ensure_warm(project_id, session_id)
    with self._lock_for(project_id):       # per-project RLock
        for ev in sess.loop.run(user_input):
            yield ev
```

`_lock_for` keeps a `threading.RLock` per project id (lazily created,
`manager.py:58`). Different projects run in parallel; sends to the same
project serialize. The HTTP layer exposes this via `try_lock()`
(`manager.py:169`) — a second concurrent send that finds the lock held
returns 409 `project_busy` immediately and **does not block an HTTP
worker thread**.

**Idempotent start**: `start_session` (`manager.py:64`) returns 200
(re-warm) rather than 409 if the supplied `session_id` already exists.
Use for "open if it exists, otherwise create".

### 4. Project templates and the v0 migration

`projects/templates.py:1` implements project templates: a directory
`templates/<name>/` containing `template.json` (metadata) plus any seed
files. `apply_template` (`templates.py:73`) copytrees them into the new
workspace. Operators can drop an external pack via `MINI_CC_TEMPLATES_DIR`.

The one-shot migration from the old flat layout (`projects/<pid>/`) to
the new tenant-scoped layout (`<data>/tenants/<tid>/projects/<pid>/`)
lives in `projects/migrate_v0_tenant_layout.py:1`. The script defaults to
dry-run; `--apply` actually moves files. Idempotent — re-running after
`--apply` is a no-op.

---

## Operation & configuration

### Key file locations

| Concern | Path |
|---------|------|
| One session's messages | `<data>/tenants/<tid>/.storage/<pid>/messages/<sid>.json` |
| Sessions index | `<data>/tenants/<tid>/.storage/<pid>/sessions/index.json` |
| Project metadata | `<data>/tenants/<tid>/projects/<pid>/meta.json` |
| Key registry | `<data>/keys.json` |
| SSE event log | `<data>/tenants/<tid>/.storage/<pid>/sessions/<sid>.events.jsonl` |

### Relevant environment variables

| Variable | Purpose |
|----------|---------|
| `MINI_CC_DATA_DIR` | The whole `<data>` root |
| `MINI_CC_TEMPLATES_DIR` | External template-pack root (overrides package `templates/`) |

### HTTP endpoints relevant to this chapter

| Method | Path | Behavior |
|--------|------|----------|
| `POST` | `/tenants/{tid}/projects` | create; 409 if exists |
| `GET` | `/tenants/{tid}/projects/{pid}/sessions` | list SessionMeta (incl. `in_memory`) |
| `POST` | `/tenants/{tid}/projects/{pid}/sessions/{sid}/resume` | warm a cold session; idempotent |
| `DELETE` | `/tenants/{tid}/projects/{pid}/sessions/{sid}` | stop + unregister + delete on-disk meta |

---

## Verification steps

```bash
# assume backend on :8002, $KEY is my_tenant's key
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"project_id":"s2","display_name":"Storage demo"}' >/dev/null

curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions \
     -H "Authorization: Bearer $KEY" -d '{"session_id":"sx"}' >/dev/null

# send one turn to produce messages
curl -N -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"hello"}' | head -5

# 1. inspect the on-disk messages file (safe_session rewrites non-alnum to _)
ls $PWD/mini_cc_data/tenants/my_tenant/.storage/s2/messages/
# → sx.json

# 2. inspect the sessions index — message_count should be > 0
cat $PWD/mini_cc_data/tenants/my_tenant/.storage/s2/sessions/index.json | python -m json.tool

# 3. after restarting the server, probe the warm path:
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/resume \
     -H "Authorization: Bearer $KEY" | python -m json.tool
# → {"session_id":"sx","message_count":N,...}

# 4. concurrent sends to the same project → second is 409 project_busy
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
     http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"concurrent 1"}' &  # intentionally not waited on
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
     http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"concurrent 2"}'
# → one 200, one 409

# 5. v0 → tenant layout migration dry-run (against a legacy data dir)
python -m mini_cc.projects.migrate_v0_tenant_layout ./legacy_data
```

```bash
# unit tests: FSStorage atomic write + corruption raise + sessions index
python -m pytest tests/test_storage_fs.py -q
```

---

## Common pitfalls / debugging

1. **Treating `StorageCorruptionError` as "session is empty".** Anti-pattern:
   a corrupted file ≠ an empty session. `load_messages` raises explicitly
   so you see disk corruption instead of silently overwriting it. If you
   hit this, check whether the process was killed mid-write.
2. **Editing `permissions.toml` has no effect.** The assembly cache
   invalidates by mtime, but only checks on `get()`; the `Project` returned
   by `create()` is not in the cache. Force a rebuild with
   `pm.invalidate(pid, tenant_id=tid)`.
3. **Slow session listing.** `list_sessions` reads `sessions/index.json`
   and does not scan `messages/`. If a project predates the index feature,
   the first read lazily rebuilds the index from `messages/*.json`
   (`storage/fs.py:112`). The rebuild is one-shot; afterwards `save_messages`
   upserts the index automatically.
4. **Deleted session but `messages/<sid>.json` is still there.** Use
   `storage.delete_session` (not just index removal) — it deletes messages,
   todos, the events log, and the index entry atomically.
   `SessionManager.remove` takes this path.
5. **Ambiguous cross-tenant delete.** `pm.delete(pid)` without
   `tenant_id` raises `ValueError("project_id ambiguous")` if the same pid
   exists under multiple tenants. HTTP routes always carry tid, so they
   don't hit this; SDK direct-use must be careful.

---

## Further reading

- Sibling chapters: [01 — Overview & Architecture](01-overview.md) ·
  [03 — Three-tier Sandbox](03-sandbox.md)
- Conceptual: [s06 — Context Compaction](../../en/s06-context-compact.md)
  (transcript snapshots depend on `write_transcript`),
  [s07 — Task System](../../en/s07-task-system.md) (tasks persist as
  `tasks/<id>.json`)
- Source: `mini_cc/storage/fs.py`, `mini_cc/projects/manager.py`,
  `mini_cc/session/manager.py`, `mini_cc/projects/layout.py`
- Advanced: `docs/mini_cc/stateful-repl.md` (REPL state persistence uses storage)
