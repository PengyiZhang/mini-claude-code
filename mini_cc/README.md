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
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions`          | start; 201 new / 200 idempotent resume; 409 if exists |
| `GET`    | `/tenants/{tid}/projects/{pid}/sessions`          | list SessionMeta (disk + in_memory flag)  |
| `DELETE` | `/tenants/{tid}/projects/{pid}/sessions/{sid}`    | stop + unregister; 404 if unknown         |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/resume` | warm a cold session; idempotent; 404 if not on disk |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/send` | stream events as SSE; auto-resumes cold sessions; 409 if project busy |
| `GET`    | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions` | list pending permission requests; `[]` if no `permissions.toml` |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions/{req_id}/decide` | body `{decision:"allow"\|"deny", message?}`; 204 / 404 / 409 |
| `GET`    | `/tenants/{tid}/projects/{pid}/files/tree?path=`  | list directory children; 400 on traversal |
| `GET`    | `/tenants/{tid}/projects/{pid}/files/content?path=` | read up to 256 KB of a text file        |
| `POST`   | `/tenants/{tid}/projects/{pid}/files/mkdir?path=` | create a directory                        |
| `POST`   | `/tenants/{tid}/projects/{pid}/files/upload?path=` | multipart upload; optional `rel_paths` form field preserves folder structure |
| `DELETE` | `/tenants/{tid}/projects/{pid}/files?path=`       | delete file or directory                  |
| `GET`    | `/tenants/{tid}/projects/{pid}/download`          | stream the project workspace as a ZIP     |

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
`<data_dir>/keys.json` (atomic rename-on-write) as a JSON map of
`key → {tenant_id, scopes, created_at, expires_at, label,
rotated_from}`. Each key supports three independent axes:

- **Scopes** — least-privilege access control. See "Authentication"
  section below for the grammar.
- **Expiry** — short-lived keys for CI / share-links.
- **Rotation** — replace a compromised key without hard-cutting
  clients via optional `--grace-hours`.

Generate, list, and rotate via the CLI:

```bash
python -m mini_cc.server keygen <tenant> [--scopes ...] [--expires-in 7d] [--label ...]
python -m mini_cc.server keys   list <tenant>
python -m mini_cc.server keys   rotate <key> [--grace-hours N] [--scopes ...] [--label ...]
python -m mini_cc.server revoke <key>
```

**Authentication (Phase D):** every route declares a required scope
via `Depends(require_scope("<resource>:<verb>"))`. Held scopes are
checked against the required; a miss returns 403 with
`details.code = "insufficient_scope"` and a
`WWW-Authenticate: Bearer scope="..."` hint. Scope grammar:

| Scope           | Allows                                   |
| --------------- | ---------------------------------------- |
| `*`             | Anything (default for migrated keys)     |
| `read:*`        | Any GET                                  |
| `write:*`       | Any non-GET                              |
| `sessions:*`    | Any method on `/sessions/*`              |
| `sessions:read` | GET on `/sessions/*`                     |
| `sessions:write`| POST/DELETE on `/sessions/*`             |
| `files:read`    | GET on `/files/*` (tree/content/download)|
| `files:write`   | POST/DELETE on `/files/*`                |

`read` ≡ GET, `write` ≡ everything else. The verb is fixed at the
HTTP layer so routes don't have to specify it explicitly.

**Migration:** pre-Phase-D `keys.json` files (bare-string values)
are auto-promoted to `KeyRecord` on first read with
`scopes=["*"]`, `expires_at=None`, `label="migrated"`. The file is
not rewritten until the next mutation, so existing fixtures stay
byte-identical.

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
| `MINI_CC_LOG_FORMAT`    | `json`           | `json` or `text` (one line per event)        |
| `MINI_CC_LOG_LEVEL`     | `INFO`           | root logger level                            |
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` | `60`    | per-tenant requests/min (token bucket)       |
| `MINI_CC_RATE_LIMIT_RPM` | (empty)         | `tenant=rpm,tenant=rpm,...` overrides        |
| `MINI_CC_METRICS_ENABLED` | `1`           | master switch for the MetricsMiddleware       |
| `MINI_CC_OTEL_EXPORTER` | (unset)         | `otlp`, `jaeger`, or `console` (else log-only)|
| `MINI_CC_OTEL_ENDPOINT` | `http://localhost:4317` | OTLP gRPC endpoint              |
| `MINI_CC_OTEL_SERVICE_NAME` | `mini-cc`    | OTel resource attribute                       |

The rate limiter is a per-tenant token bucket: capacity = RPM (so a
fresh tenant can burst a full minute of calls at once), refill =
RPM/60 per second. Applies to `POST /sessions/{sid}/send` and
`POST /projects` (the resource-consuming endpoints). 429 carries a
`Retry-After` header.

Every response gets an `X-Trace-Id` header (UUID4 hex, or the inbound
one if you set `X-Trace-Id` on the request). The same id flows
through `contextvars` into the JSON log lines, so correlating a
request to its log entries is just `grep <trace_id>`.

Programmatic config:

```python
from mini_cc import AnthropicConfig, set_default_config
set_default_config(AnthropicConfig(
    api_key="sk-ant-...",
    primary_model="claude-opus-4-7",
))
```

---

## Observability (Phase E)

mini_cc ships two metric endpoints and an opt-in tracing layer. Both
inherit the trusted-network model (server binds 127.0.0.1 by default,
no auth on `/metrics`).

**Metric endpoints**:

- `GET /metrics` — Prometheus 0.0.4 text format. Point a Prometheus
  scraper at it.
- `GET /metrics.json` — JSON snapshot of the same data; easier to
  consume from the web UI or admin scripts.

**Metric catalog**:

| Metric | Type | Labels | Source |
| ------ | ---- | ------ | ------ |
| `http_requests_total` | counter | method, route_template, status, tenant | MetricsMiddleware |
| `http_request_duration_seconds` | histogram | method, route_template, tenant | MetricsMiddleware |
| `http_in_flight_requests` | gauge | — | MetricsMiddleware |
| `anthropic_tokens_total` | counter | tenant, kind (input/output/cache_read/cache_create) | AgentLoop |
| `anthropic_request_total` | counter | tenant, status (success/error/cancelled) | AgentLoop |
| `anthropic_request_duration_seconds` | histogram | tenant | AgentLoop |

`route_template` uses FastAPI's `{tid}`/`{pid}`/`{sid}` form (not the
resolved URL), so cardinality is bounded. `tenant` defaults to
`unknown` for unauthenticated routes (health, the metrics endpoints
themselves).

**Tracing model**:

- Default mode is *log spans*: each `log_span(name, **fields)` block
  in `mini_cc.server.tracing` emits a structured `span.end` INFO log
  line on exit, carrying `span`, `dur_ms`, `trace_id`, and the
  caller-supplied fields. Zero extra deps.
- Setting `MINI_CC_OTEL_EXPORTER=otlp` (or `jaeger` / `console`)
  lazily imports the OpenTelemetry SDK and emits real OTel spans in
  addition to the log lines. Missing `opentelemetry-*` packages →
  warning + fall back to log-only mode.

**Token attribution**:

After each Anthropic stream completion, `AgentLoop.run()` reads
`response.usage` and pushes four counters
(`input`/`output`/`cache_read`/`cache_create`) per tenant. Cancellations
increment `anthropic_request_total{status="cancelled"}` instead.

**Quickstart with Prometheus**:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: mini_cc
    static_configs:
      - targets: ["localhost:8000"]
```

```bash
# Drive some traffic and watch counters tick
python -m mini_cc.server &
for i in $(seq 1 5); do curl -s localhost:8000/health >/dev/null; done
curl -s localhost:8000/metrics | grep http_requests_total
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

## Session lifecycle (resume across restarts)

A session is **cold** when it exists on disk but no `AgentLoop` is
built for it in the current process, and **warm** once an `AgentLoop`
holds its transcript in memory. Messages, todos, and the session
index live on disk under `<state_root>/<project_id>/`; `AgentLoop`
reloads them on construction.

Three resume paths:

1. **Auto-resume on send.** `POST /sessions/{sid}/send` warms a cold
   session transparently — no extra round-trip. 404 only if neither
   in-memory nor on-disk.
2. **Idempotent start.** `POST /sessions` with an existing
   `session_id` returns **200** (re-warm) instead of 409. Use for
   "open this session if it exists, otherwise create it".
3. **Explicit resume.** `POST /sessions/{sid}/resume` warms a cold
   session and returns its `SessionMeta`. Useful for priming before
   the first send (e.g. warming a freshly-restarted server).

`GET /sessions` returns `SessionMeta` for every on-disk session:

```json
[{
  "session_id": "s1",
  "created_at": "2026-06-20T22:11:08.110Z",
  "last_active_at": "2026-06-20T22:14:42.009Z",
  "message_count": 14,
  "in_memory": true
}]
```

The index lives at `<state_root>/<project_id>/sessions/index.json`.
For projects created before this feature, the index is rebuilt
lazily from the `messages/*.json` files on first read.

**Mid-turn crash recovery:** if the server died mid-turn, the
transcript's tail may be an assistant message with `tool_use` blocks
that never got their `tool_result`. On warm-load, the loop appends a
synthetic user turn with one `tool_result` per dangling id, content
`[interrupted by server restart]`, `is_error: true`. This unblocks
the model without losing prior context.

---

## Interactive permissions

Opt-in per project. Create
`<workspace>/.mini_cc/permissions.toml`:

```toml
prompt_tools = ["bash", "fs_write", "fs_edit"]
timeout_seconds = 300   # optional; default 300
```

When the loop is about to call a tool listed in `prompt_tools`, it:

1. Emits a `permission_request` event to the SSE stream:
   ```json
   {"type": "permission_request", "request_id": "<hex>",
    "tool_name": "bash", "tool_input": {"command": "..."},
    "id": "<tool_use_id>"}
   ```
2. Blocks until the client POSTs a decision to
   `/permissions/{req_id}/decide`, the timeout elapses, or the
   session stops.
3. On `allow` → runs the tool. On `deny` → the `message` (or
   `[permission timed out]`) becomes the `tool_result` content shown
   to the model.

Clients can recover after a reconnect via
`GET /permissions` which lists currently-pending requests.

Without the config file: no interceptor, no prompts, today's
behavior. The static `make_permission_hook` (deny-list + destructive
gate) still applies independently.

---

## Testing

The framework ships with 289 passing tests + 24 subtests (pytest).
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

## Web UI (P4)

A Devin-style dark-themed React frontend lives under `mini_cc/web/`. It
exercises the same HTTP/SSE surface documented above — no separate API.

**Stack**: Vite + React 19 + TypeScript + Tailwind + Zustand + React
Router. Playwright drives the e2e suite.

### Running locally

The dev backend listens on `127.0.0.1:8002` (8001 was occupied by
another service on the dev machine; the frontend's default API base
follows suit). A LiteLLM Anthropic-compatible proxy on `:8000`
proxies model calls to e.g. `glm-4.7`.

```bash
# one-time
pip install -r requirements.txt              # adds python-multipart for uploads
cd mini_cc/web && npm install

# terminal 1 — backend (port 8002)
python -m mini_cc.server keygen my_tenant    # prints mck_<hex>
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
MINI_CC_ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
MINI_CC_ANTHROPIC_API_KEY=any-fake-key \
python -m mini_cc.server

# terminal 2 — frontend (port 5173)
cd mini_cc/web && npm run dev
```

Open http://localhost:5173 and sign in with the `mck_<hex>` key from
`keygen`. The login form picks the tenant automatically from the key.

### Features

- **Tenant switcher** in the top bar; profiles persist in `localStorage`.
  Manage / reveal / remove keys on the Tenants page.
- **Project list** with create / delete / open.
- **Workspace** with three tabs:
  - **Chat** — SSE-streamed assistant replies, foldable activity cards
    (tool_use / tool_result) that default-collapsed, click to expand.
    Inline **permission prompts** render when the agent requests a
    tool call gated by `permissions.toml` (with optional countdown).
    The sidebar session list shows a **cold/warm dot** per session;
    hovering exposes a "warm" button that POSTs `/sessions/{sid}/resume`
    to load the on-disk session into memory.
  - **Files** — recursive tree with a right-click context menu: upload
    files, upload folder (preserves structure via `webkitdirectory`),
    new folder, preview, delete. Download project as ZIP.
  - **Sessions** — list, open, remove.

### Admin UI (Phase F)

The **admin** entry in the top bar opens a separate auth flow that
requires a `*`-scoped key. Routes are prefixed `/admin/*` and have
their own `localStorage` slot (`mini_cc.admin.v1`), so admin and chat
sessions can coexist.

- **AdminLogin** — tenant + key form. Probe is `GET /tenants/{tid}/admin/keys`,
  which needs `admin:read`. Insufficient-scope keys are rejected with a
  visible error.
- **AdminKeys** — full key lifecycle: list, create, edit (PATCH scopes /
  label / expiry), rotate (with optional `grace_hours`), revoke. Scope
  chips are color-coded (`*` → red, `admin:read` → amber, `read:*` → sky…).
- **AdminMetrics** — refreshes `/tenants/{tid}/admin/metrics.json` every
  5s and renders: per-route HTTP request counts, sparkline of req/min,
  token counters (input / output / cache_read / cache_create), Anthropic
  request status table, and bucket-distribution bars for the HTTP and
  Anthropic latency histograms.

### Playwright e2e

```bash
cd mini_cc/web
MINI_CC_DATA_DIR=$PWD/../mini_cc_data_e2e npm run e2e
```

`e2e/globalSetup.ts` provisions a fresh `e2e` tenant, a default-scoped
key, **and** a `*`-scoped admin key, plus the `e2e_proj` project against
the running backend. Specs cover auth, project creation, file upload +
preview + zip download, a streamed chat turn through LiteLLM, and the
admin flow (login reject for non-admin keys, key listing, new-key
creation, metrics dashboard render).

---

## Out of scope (deferred by design)

These are **not** bugs — they were deliberately cut. File an issue
before picking them up so we can align on scope.

- **WebSocket transport.** SSE only for now.
- **Container-level sandbox isolation (P5).** Today's sandbox is a
  defense-in-depth soft layer; for untrusted code, run mini_cc inside
  a container.

---

## License

MIT.
