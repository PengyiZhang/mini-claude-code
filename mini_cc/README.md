# mini_cc — backend-integrable agent framework

Multi-tenant, sandboxed, backend-integrable mini Claude Code. A
reference port of the 19-subsystem harness pattern from
`s20_comprehensive/`, restructured for production multi-tenant use:
per-project isolation, pluggable storage, no module globals, and an
optional HTTP/SSE transport.

The model is the driver; this package is the vehicle. Use it as an SDK
in a Python backend, or stand up the server and drive it from any
language.

```
Agency comes from the model. mini_cc gives the model hands, eyes,
a workspace, teammates, and a network surface.
```

---

## Layered architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  server/ (FastAPI, HTTP/SSE, per-tenant API-key auth)           │  ← transport
├─────────────────────────────────────────────────────────────────┤
│  session/  SessionManager (project-scoped locking)              │  ← orchestration
│  projects/ ProjectManager (tenant metadata, layout)             │
├─────────────────────────────────────────────────────────────────┤
│  core/     AgentLoop, hooks, recovery, compaction, subagent     │  ← agent core
│  teams/    MessageBus, ProtocolTracker, TeammateSpawner         │
│  tools/    bash, fs, todo, cron, task, worktree, mcp, ...       │
│  skills/   on-demand skill loading                              │
│  mcp/      MCP client/pool                                      │
│  scheduler/ CronScheduler                                       │
├─────────────────────────────────────────────────────────────────┤
│  sandbox/  SubprocessSandbox + Policy                           │  ← isolation
│  storage/  pluggable Storage interface, FSStorage default       │
│  auth/     TenantKeyRegistry (transport-agnostic)               │
└─────────────────────────────────────────────────────────────────┘
```

Each layer imports only the layers below it. SDK core has no
knowledge of the HTTP transport. Storage and sandbox depend on nothing
above them. No module-level global state — every subsystem instance
is per-project.

---

## Package layout

```
mini_cc/
  __init__.py            public API surface
  config.py              AnthropicConfig (env-driven)
  _shim.py               backward-compat agent_loop() for s20 readers

  storage/               Storage Protocol + FSStorage
  sandbox/               Sandbox base + SubprocessSandbox + Policy
  auth/                  TenantKeyRegistry (JSON-backed, thread-safe)

  core/
    loop.py              AgentLoop + ProjectRef
    system_prompt.py     runtime section-based prompt assembly
    compaction.py        layered context compaction + transcript snapshot
    recovery.py          retry / token escalation / model fallback
    hooks.py             per-project Hooks registry + 4 hook factories
    subagent.py          spawn_subagent (focused single-purpose loops)

  tools/                 one file per tool family:
    bash.py  fs.py  todo.py  cron.py  skills.py
    task.py  worktree.py  background.py  mcp.py  teams.py  subagent.py
    base.py              ToolContext / FunctionTool / Tool protocol
    __init__.py          builtin_tools() registry

  skills/                SkillLoader + on-demand load_skill tool backing
  mcp/                   MCPClient + per-project MCPPool
  scheduler/             per-project CronScheduler
  teams/                 MessageBus + ProtocolTracker + TeammateSpawner

  projects/              ProjectManager + ProjectMeta + on-disk layout
  session/               SessionManager (per-project RLock serialization)

  server/                P3 HTTP/SSE transport
    app.py               build_app() — FastAPI factory + lifespan + CORS
    sse.py               sync Iterator → async SSE bridge
    deps.py              require_tenant / validate_id dependencies
    errors.py            MiniCCError hierarchy + envelope mapping
    schemas.py           pydantic request/response models
    routes/
      projects.py        CRUD under /tenants/{tid}/projects
      sessions.py        start / list / remove / send (SSE)
    cli.py               python -m mini_cc.server  [serve|keygen|keys|revoke]
    __main__.py          entry
```

---

## Public API quick start

### As a Python SDK (in-process)

```python
from mini_cc import ProjectManager, SessionManager

pm = ProjectManager("/data/projects")
pm.create(tenant_id="t1", project_id="demo")
sm = SessionManager(pm)
sess = sm.start_session("demo")

for ev in sm.send("demo", sess.session_id, "list the files here"):
    if ev["type"] == "text":
        print(ev["text"])
    elif ev["type"] == "tool_use":
        print(f"→ {ev['name']}({ev['input']})")
    elif ev["type"] == "tool_result":
        print(f"← {ev['content'][:80]}")
    elif ev["type"] == "done":
        break
```

Events yielded by `SessionManager.send()`:

| type                      | payload                                          |
| ------------------------- | ------------------------------------------------ |
| `text`                    | `{text: str}`                                    |
| `tool_use`                | `{name, input, id}`                              |
| `tool_result`             | `{tool_use_id, content}`                         |
| `done`                    | —                                                |
| `error`                   | `{message}`                                      |
| `max_tokens_escalation`   | `{max_tokens}`                                   |
| `cron_fired`              | `{job_id, prompt}`                               |
| `background_notification` | —                                                |

### Over HTTP/SSE (any language)

```bash
# Provision a tenant key
python -m mini_cc.server keygen tenant1     # → mck_<32hex>

# Start the server
MINI_CC_DATA_DIR=/var/lib/mini_cc \
MINI_CC_HOST=0.0.0.0 MINI_CC_PORT=8000 \
python -m mini_cc.server
```

```bash
# Create a project
curl -X POST http://localhost:8000/tenants/tenant1/projects \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"project_id":"demo","display_name":"Demo"}'

# Start a session
curl -X POST http://localhost:8000/tenants/tenant1/projects/demo/sessions \
     -H "Authorization: Bearer mck_<key>" \
     -d '{"session_id":"s1"}'

# Send a turn — streams SSE
curl -N -X POST \
     http://localhost:8000/tenants/tenant1/projects/demo/sessions/s1/send \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"user_input":"hello"}'
```

SSE wire format:

```
data: {"type":"text","text":"hi there"}\n\n
data: {"type":"tool_use","name":"bash","input":{"command":"ls"},"id":"tu1"}\n\n
data: {"type":"tool_result","tool_use_id":"tu1","content":"file1\nfile2"}\n\n
data: {"type":"done"}\n\n
data: [DONE]\n\n
```

---

## HTTP endpoints

All routes under `/tenants/{tid}/...` — the `{tid}` in the URL **must**
match the tenant_id resolved from the bearer API key, else 403.

| Method   | Path                                              | Behavior                                  |
| -------- | ------------------------------------------------- | ----------------------------------------- |
| `GET`    | `/health`                                         | no-auth liveness probe                    |
| `POST`   | `/tenants/{tid}/projects`                         | create; 409 if exists; 400 on bad id      |
| `GET`    | `/tenants/{tid}/projects`                         | list projects for this tenant             |
| `GET`    | `/tenants/{tid}/projects/{pid}`                   | get; 404 if missing or cross-tenant       |
| `DELETE` | `/tenants/{tid}/projects/{pid}`                   | remove; 404 if missing                    |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions`          | start a session                           |
| `GET`    | `/tenants/{tid}/projects/{pid}/sessions`          | list session_ids                          |
| `DELETE` | `/tenants/{tid}/projects/{pid}/sessions/{sid}`    | stop + unregister; 404 if unknown         |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/send` | stream events as SSE; 409 if project busy |

Error envelope (one shape for every error):

```json
{
  "error": {
    "code": "not_found",
    "message": "project demo not found",
    "details": {}
  }
}
```

Status code mapping: `KeyError → 404`, `ValueError("already exists")
→ 409`, `ValueError(...) → 400`, unknown → 500. Project-busy 409
includes `details.code = "project_busy"`.

---

## Security model

**Sandbox (per-project):** `SubprocessSandbox` enforces:
- Path validation — all read/write/edit/glob/grep paths resolved under
  `project_root`; escapes (including `..` and absolute) rejected.
- Command policy — bash commands scanned against `Policy`; deny-list
  matches raise `CommandBlockedError`.
- Filtered env — only `Policy.allowed_env` keys passed; `HOME` and
  `USERPROFILE` rewritten to the project workspace.

**ID validation:** project_id and session_id must match
`[A-Za-z0-9_-]+` (enforced both at the SDK layer and the HTTP layer).
This closes the path-traversal hole in `ProjectManager.delete`.

**Tenant isolation:** every read/write route verifies the resource's
`tenant_id` matches the path `{tid}` (which itself was authenticated
via the API key). Cross-tenant access returns 404 (not 403) so the
existence of another tenant's project is not leaked.

**API keys:** one key per tenant. Keys are stored in
`<data_dir>/keys.json` with atomic rename-on-write. Generate via
`python -m mini_cc.server keygen <tenant_id>`. Revoke via
`python -m mini_cc.server revoke <key>`.

**Soft sandbox, not containerized:** the sandbox is a defense-in-depth
layer, not a hard security boundary. For untrusted code, run mini_cc
inside a container or VM (P5 work, not yet shipped).

---

## Configuration

Environment variables (read by `python -m mini_cc.server`):

| Variable                | Default          | Purpose                                     |
| ----------------------- | ---------------- | ------------------------------------------- |
| `ANTHROPIC_API_KEY`     | —                | Anthropic API key (SDK uses it directly)    |
| `ANTHROPIC_BASE_URL`    | —                | alternate base URL (proxies, Anthropic-compatible) |
| `MODEL_ID`              | `claude-sonnet-4-6` | primary model                            |
| `FALLBACK_MODEL_ID`     | —                | fallback model used on failure              |
| `MINI_CC_DATA_DIR`      | `./mini_cc_data` | root for projects, state, key registry      |
| `MINI_CC_HOST`          | `127.0.0.1`      | server bind address                          |
| `MINI_CC_PORT`          | `8000`           | server port                                  |
| `MINI_CC_CORS_ORIGINS`  | (empty)          | comma-separated allowed origins for browser SSE |

Programmatic config:

```python
from mini_cc import AnthropicConfig, set_default_config
set_default_config(AnthropicConfig(
    api_key="sk-ant-...",
    primary_model="claude-opus-4-7",
))
```

---

## Subsystem map

Every subsystem below is per-project (no module globals). The
ProjectManager wires them together in `Project._assemble()`.

| Subsystem              | Purpose                                              | s20 ref       |
| ---------------------- | ---------------------------------------------------- | ------------- |
| `AgentLoop`            | streams events from one user turn                    | s01, s20      |
| `SubprocessSandbox`    | path whitelist + command policy + filtered env       | s03           |
| `Hooks`                | UserPromptSubmit / PreToolUse / PostToolUse / Stop   | s04           |
| `make_permission_hook` | deny-list + destructive-command gate                 | s03           |
| `make_log_hook` etc.   | audit / large-output / all-events sinks              | s04           |
| TodoWrite              | plan-first execution                                 | s05           |
| `spawn_subagent`       | focused single-purpose loops with restricted tools   | s06           |
| `SkillLoader`          | on-demand skill expansion                            | s07           |
| Compaction             | tool_result_budget / snip / micro / compact_history  | s08           |
| Memory                 | per-project persistent memory file                   | s09           |
| `assemble_system_prompt` | section-based runtime assembly                      | s10           |
| `RecoveryState`        | retry / token escalation / fallback model            | s11           |
| Task system            | subject + deps + status + owner + worktree binding   | s12           |
| `BackgroundScheduler`  | slow ops offloaded; notifications land next turn     | s13           |
| `CronScheduler`        | per-project cron jobs                                | s14           |
| `MessageBus`           | per-project JSONL mailboxes                          | s15           |
| `ProtocolTracker`      | plan-approval + shutdown handshakes                  | s16           |
| `TeammateSpawner`      | threads; idle-poll auto-claim; plan-approval gate    | s17           |
| Worktree + `wt_ctx`    | per-task worktree; teammate sandbox auto-cwd on claim| s18           |
| `MCPPool`              | per-project MCP clients; tools merged into pool      | s19           |
| Transcript-on-compact  | `write_transcript` snapshot before discard           | s20           |

**Plan-approval gate** is enforced at the turn boundary (not
mid-turn) — `submit_plan` tells the model to end its turn and the
spawner blocks the next turn until `review_plan` arrives. Documented
simplification vs s20.

**`wt_ctx` auto-cwd**: when a teammate auto-claims a task with a
worktree, `AgentLoop.set_worktree(path)` swaps the teammate's sandbox
to the worktree. Teammates get their own `SubprocessSandbox` at spawn
so the swap doesn't affect other sessions.

---

## Concurrency model

- `SessionManager.send()` is **synchronous** and acquires a per-project
  `RLock`. Different projects run in parallel; sends to the **same**
  project serialize.
- The HTTP layer exposes this via `SessionManager.try_lock()` — a
  second concurrent send to the same project returns **409
  `project_busy`** instead of blocking an HTTP worker.
- SSE streams bridge the sync iterator through a dedicated worker
  thread + asyncio.Queue. Client disconnect calls `session.stop()`;
  the loop exits at the next iteration boundary.

---

## Testing

The framework ships with 220 passing tests + 24 subtests (pytest).
Mirrors of s20's mocking patterns live in `tests/test_p0_*.py`.

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

Each P3 server test uses FastAPI's `TestClient` with a stub Anthropic
client, so no network and no real API calls. Tests cover:

- Auth: missing/unknown key (401), tenant mismatch (403)
- Project CRUD: create / list / get / delete / duplicate (409)
- ID validation: traversal attempts rejected (400)
- Cross-tenant isolation: another tenant gets 404 (no leak)
- Sessions: start / list / remove (404 on unknown)
- SSE: event ordering, error event forwarding, generator-exception
  cleanup, sentinel
- Concurrency: 409 `project_busy` when project lock is held

---

## Out of scope (deferred by design)

These are **not** bugs — they were deliberately cut. File an issue
before picking them up so we can align on scope.

- **WebSocket transport.** SSE only for now.
- **Session resume across server restarts.** Sessions live in process
  memory; project state (messages, todos, tasks) persists on disk and
  survives restart, but live `AgentLoop` instances do not.
- **Mid-turn cancellation of in-flight Anthropic API calls.** Current
  cancellation (client disconnect / `session.stop()`) takes effect at
  the next iteration boundary. The in-flight HTTP call to Anthropic
  completes.
- **Interactive permission prompts.** s20 prompts the operator via
  `input()`; an SDK / server context can't. `make_permission_hook`
  returns a non-interactive gate by default; apps needing interactive
  prompts register their own.
- **Per-tenant rate limiting.**
- **Container-level sandbox isolation (P5).** Today's sandbox is a
  defense-in-depth soft layer; for untrusted code, run mini_cc inside
  a container.
- **`pyproject.toml`.** None today; dependencies live in
  `requirements.txt` and the entry is `python -m mini_cc.server`.

---

## License

MIT.
