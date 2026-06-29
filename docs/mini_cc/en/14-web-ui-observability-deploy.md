[ < [13](13-teams-scheduler.md) ] · [中文 version](../zh/14-web-ui-observability-deploy.md)

# 14 — Web UI, Observability & Deployment

> Advanced internals, chapter 14 (the final chapter). The previous twelve chapters dissected the SDK core. This chapter closes the loop on the shell around that core: the **React web UI**, **end-to-end observability** (structured logs, trace_id, metrics, spans), and **deployment shape** (process startup, data directory, backend tier, single-origin frontend). By the end you should be able to bring mini_cc up and trace a request from the browser down to an LLM call.

---

## Problem & motivation

The SDK core (`core/`, `tools/`, `projects/`) is deliberately unaware of HTTP or UI — you can embed it purely as an SDK. But the moment you hand a multi-tenant, browser-drivable agent framework to real humans, four questions emerge the SDK refuses to answer:

1. **How do you pipe an SSE stream into a browser?** `EventSource` only supports GET and cannot carry a Bearer token; `/send` is POST + streaming response — you need a hand-rolled fetch + `ReadableStream` client.
2. **How do you "see" a running workflow?** A Workflow V2 run is not an LLM stream; it is a sequence of discrete steps (action / validate / checkpoint / webhook_wait / email_wait), some pausing for human approval. The UI must let the user know at any moment "where it's stuck, what's next, what the last step produced."
3. **How do you trace a slow request?** A send crosses middleware → deps auth → SessionManager → AgentLoop → tools → SSE bridge; if every log line speaks for itself, nothing stitches together.
4. **How do you go from "the source runs" to "it runs in production"?** One command for the backend, one for the frontend, an env var to switch sandbox backend, the `dist/` static assets served from the same FastAPI process — deployment should approach "single-process full-stack."

`mini_cc/web/` answers 1-2; `mini_cc/server/{logging_config,middleware,tracing,metrics}.py` answers 3; `mini_cc/server/{cli,app,runtime_context}.py` answers 4.

---

## Design & implementation

### A. Web UI: a hash-routed React app

The frontend is a Vite + React 19 + react-router-dom 7 + zustand single-page app, source under `mini_cc/web/src/`. Routing uses **HashRouter**, not BrowserRouter, because in production the frontend is mounted same-origin by FastAPI (`app.py:227-247` SPA fallback) and `#/projects/...` anchor routing lets deep links work without server-side cooperation.

The entry `main.tsx:15-30` defines all eight routes: `/` (Login), `/tenants`, `/projects`, `/projects/:pid` (Workspace), `/projects/:pid/workflows` (WorkflowV2), `/admin/login`, `/admin/keys`, `/admin/metrics`. `App.tsx:8-40` runs two independent auth gates — **regular tenant users** via the `useAuth` store, **admins** via the `useAdmin` store: two tokens, two sessions, `/admin/*` through the admin gate. Unauthenticated access to a protected route always `<Navigate to="/" replace />`.

The three main pages:

- **Workspace** (`pages/Workspace.tsx:36`) — the project's primary conversation interface, three tabs: `chat` (message stream + slash commands + permission prompts), `files` (FileTree + FilePreview), `run` (RunTablePanel). SSE stream responses enter the zustand chat store through `streamSend` (`lib/sse.ts:42`).
- **WorkflowV2** (`pages/WorkflowV2.tsx:27`) — the workflow visual editor and executor (W6); detailed below.
- **Admin** (`pages/AdminKeys.tsx` / `pages/AdminMetrics.tsx`) — cross-tenant key management plus a metrics dashboard. AdminMetrics pulls a snapshot every 5 seconds (`REFRESH_MS = 5000`, `AdminMetrics.tsx:7`) and renders RPS, token rates via Sparkline.

### B. Workflow V2: the three-pane visual editor

`WorkflowV2.tsx:27` is built around a fixed three-pane layout; the component tree lives entirely under `web/src/components/workflowV2/`: left pane Definitions + Runs, middle pane chat-like execution timeline, right pane step inspector. Four components:

| File | Role |
|---|---|
| `LeftPane.tsx:45` | Left pane: definitions list (click to select, ✎ edit, ＋ new) plus runs filtered by the current def; each carries a `STATUS_BADGE` color (`pending/running/paused/completed/failed/cancelled`, `LeftPane.tsx:27-34`) |
| `RunView.tsx:44` | Middle pane: execution timeline. Each step is one row `[idx] step_id (type)` with a status glyph (`✓ ✗ ⏸ ▶ ⏭`); `previewOutput()` truncates to 240 chars (`RunView.tsx:424`). A `paused` checkpoint step renders inline Approve/Reject + optional feedback (`RunView.tsx:356-386`) |
| `StepInspector.tsx:24` | Right pane: full definition + run-time state of the selected step — prompt, condition, config, `inputs_schema`, `outputs_schema`, plus `started_at`/`completed_at`/`output` (pretty JSON)/`error` |
| `DefinitionEditor.tsx:1` | Slide-out modal: create/edit definitions, steps add/remove/reorder inline, per-step-type config fields surface as the type changes; edit mode adds 🗑 delete (with `window.confirm`) |

**Polling strategy**: the backend does not yet expose a dedicated SSE channel for workflow events — a run advances synchronously via POST `/drive` or is resolved asynchronously by external webhook/email (`workflowV2Store.ts:1-9` comment). So the frontend uses `startRunPolling()` (`workflowV2Store.ts:97-118`): while the selected run is non-terminal, every **2 seconds** it pulls `getWorkflowV2Run` and merges into the store. When it reaches a terminal state (`completed/failed/cancelled`, `isTerminalStatus()` at `workflowV2Store.ts:120`), polling stops. This is a deliberate "good enough" tradeoff — a future SSE push only needs to swap `startRunPolling` for a subscription; `RunView` is unchanged since it reads the run only from the store.

#### The "drive with no session" UX fix and the amber banner pattern

When an action step dispatches, the backend injects the prompt into some chat session's AgentLoop. If the project has no session at all, `drive()` reports `"no session available"` client-side (`RunView.tsx:100-104`). One step earlier: on mount `RunView` probes via `listSessionMetas()` (`RunView.tsx:61-71`) and **if no session exists, renders an amber banner across the top of the middle pane** plus a one-click shortcut to the chat tab (`RunView.tsx:199-214`):

```tsx
{sessionsLoaded && !sessionId && !isTerminalStatus(run.status) && (
  <div className="border-b border-border bg-amber-50 dark:bg-amber-900/20 ...">
    <span>action steps need a chat session to dispatch — none exist in this project yet</span>
    <a href={`#/projects/${pid}`}>open chat tab →</a>
  </div>
)}
```

A classic "translate a backend constraint into a UI nudge": no error, no interruption — just a banner routing the user to the page where the problem can be solved. Matching fix in `e2e/NOTES.md` commit `7aa4fab`.

### C. SSE consumption: fetch + ReadableStream + auto-reconnect

The chat-stream client lives in `lib/sse.ts`. **It does not use `EventSource`** — EventSource cannot POST a body or send an `Authorization` header. `streamSend()` (`sse.ts:42`) issues `fetch()` POST + `res.body.getReader()` and manually parses the `id: <seq>\ndata: <json>\n\n` framing, recognizing the `data: [DONE]` sentinel to end the stream.

The B8 **mid-stream auto-reconnect** (`sse.ts:60-184`): once the stream breaks mid-flight and at least one event has been received, the client retries with exponential backoff (1s → 2s → 4s, default max 3 attempts). Each retry carries a `Last-Event-Id: <last_seq>` header (tells the server where to replay from) and `resume: true` in the body (triggers the server's **resume-only path**, skipping the lock and LLM dispatch and replaying only from the per-session event log, `sse.ts:65-76`). That `resume=true` semantic is the critical fix (`NOTES.md` commit `aaa5793`): earlier reconnects re-triggered a **duplicate LLM dispatch**; the `resume` gate turns reconnection into pure "replay undelivered events" — no tokens re-spent. 4xx (other than 409) are deterministic and not retried; 5xx and 409 `project_busy` are transient and retried (`sse.ts:111`).

### D. Observability: structured logs + trace_id + spans + metrics

**Structured logging** (`logging_config.py`). `configure_logging()` (`logging_config.py:165-192`) installs a single `StreamHandler` on the root logger in **JSON** by default (one object per line: `ts` ISO8601 ms, `level`, `logger`, `msg`, `trace_id`, `tenant`, plus `extra={...}` passed through); `MINI_CC_LOG_FORMAT=text` switches to human-readable, idempotent. `JsonFormatter.format()` (`logging_config.py:123-145`) explicitly excludes Python's reserved `LogRecord` fields (`_RESERVED`) and expands the remaining `extra` into the JSON — so `logger.info("send.dispatched", extra={"pid":..., "dur_ms":...})` in a route flows straight into an aggregation system. **RedactingFilter** (`logging_config.py:48-83`) is the B9 safety filter: attached to the handler, before each record lands it scrubs credential-shaped substrings via `_REDACT_PATTERNS` (`logging_config.py:32-45`): `mck_...` → `[REDACTED:mck_key]`, `Bearer xxx` → `Bearer [REDACTED]`, `sk-ant-...` → `[REDACTED:anthropic_key]`, `api_key=...` → `api_key=[REDACTED]`. It rewrites `record.msg`, `record.args`, and `record.exc_text` simultaneously so any formatting path sees the scrubbed string.

**Request-scoped trace_id + tenant** (`middleware.py`). `TraceIdMiddleware` (`middleware.py:25-43`) is the **outermost** middleware (rationale in `app.py:127`'s comment): every inbound request accepts the client `X-Trace-Id` header or `new_trace_id()`s one; reads the tenant from `path_params["tid"]`; both values go into contextvars (`_TRACE_CTX`/`_TENANT_CTX`, `logging_config.py:24-25`). The contextvars benefit is **propagation** — any `logger.xxx()` in the same thread/async task reads `_TRACE_CTX.get()` without explicit threading, and `JsonFormatter` automatically adds the trace_id to the JSON. The finally block clears the contextvars to prevent worker-thread leakage across requests (`middleware.py:38-41`); the response also echoes `X-Trace-Id` (`middleware.py:42`) for client correlation.

**End-to-end tracing** (tenant → project → session → step). The observable path of one send:

```
browser POST /send
  → TraceIdMiddleware: set_trace_id(<hex>), set_tenant("demo")
  → deps.py: require_scope("sessions:write")
  → routes/sessions.py: SessionManager.send(pid, sid, input)
       ↓ logger.info("send.dispatched", extra={"pid": pid, "sid": sid})
       ↓ AgentLoop.run(input) → yield event
            ↓ tracing.log_span("tool.bash", tool="bash", cmd=...) wraps tool calls
  → SSE bridge (sse.py) serializes the event into id: N / data: {...} frames
  → client sse.ts parses → into the zustand chat store
```

Chase a slow request by filtering your log aggregator on `trace_id=<that hex>`: every line from `send.dispatched` to `span.end span=tool.bash dur_ms=...` shows up. `tracing.py:85-132`'s `log_span(name, **fields)` logs only when OTel is off; setting `MINI_CC_OTEL_EXPORTER` also opens a real OTel span exported to otlp/jaeger/console, downgrading gracefully if the SDK is missing (`tracing.py:38-49`).

**Metrics + run history**. `MetricsMiddleware` (`middleware.py:46-87`) is a raw ASGI middleware (not `BaseHTTPMiddleware`) so it can read the FastAPI route template from `scope["route"]` via `_extract_route_template()` (`middleware.py:89-97`) and count per `(method, route_template, status, tenant)` combination. RED trio: `http_requests_total` (counter), `http_request_duration_seconds` (histogram), `http_in_flight_requests` (gauge). Exports: `GET /metrics` (Prometheus text) and `GET /metrics.json` (JSON snapshot, `app.py:185-197`), both labeled "trusted-network only." Run history and step output surface through the W1-W6 REST APIs (`GET /workflow-definitions`, `/workflow-runs`, `/workflow-runs/{run_id}`); `RunView` renders them as a timeline with inline output previews, StepInspector pretty-prints `output`/`error`/`started_at`/`completed_at`. This is workflow observability — no SSE, but full run-state persistence plus 2-second polling.

### E. Deployment: from source to production

**E1. Bringing up**. `python -m mini_cc.server` is equivalent to the `serve` subcommand; frontend dev mode is `cd mini_cc/web && npm run dev` (vite, default 5173). `cli.main()` (`cli.py:367-386`) defaults to `cmd_serve` (`cli.py:56-140`) when no subcommand is given, which configures logging, creates `data_dir`, probes docker, builds `ServerRuntimeContext`, wires up managers, then `build_app()` and `uvicorn.run`. `.env` is auto-loaded by `python-dotenv` at the top of `main()` (`cli.py:372-379`); missing dotenv degrades to "read os.environ."

**E2. Environment variables** (deployment-relevant):

| Variable | Default | Source |
|---|---|---|
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | `cli.py:22-23` |
| `MINI_CC_HOST` / `MINI_CC_PORT` | `127.0.0.1` / `8000` | `cli.py:118-119` |
| `ANTHROPIC_API_KEY` / `LITELLM_API_KEY` | — | config |
| `MINI_CC_SANDBOX_BACKEND` | `auto` (`auto`/`opensandbox`/`docker`) | `runtime_context.py:64` |
| `MINI_CC_LOG_FORMAT` / `MINI_CC_LOG_LEVEL` | `json` / `INFO` | `cli.py:69-70` |
| `MINI_CC_CORS_ORIGINS` | `*` | `cli.py:111-112` |
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` / `MINI_CC_RATE_LIMIT_RPM` | `60` / — | `cli.py:36-38` |
| `MINI_CC_SHARE_SECRET` / `MINI_CC_WEBHOOK_SECRET` | dev-fallback (restart-invalidating, loud startup warning) | `app.py:38-54` |
| `MINI_CC_OTEL_EXPORTER` | — (`otlp`/`jaeger`/`console`) | `tracing.py:34` |
| `MINI_CC_WEB_DIST` | auto-detected | `app.py:210` |

On startup `cmd_serve` logs a `starting server` line (`cli.py:122-129`) carrying `host/port/data_dir/docker_available/sandbox_backend`; if LLM credentials are missing, an additional `warning` says "`/send` will fail with 401" (`cli.py:133-138`).

**E3. Data directory**. `<MINI_CC_DATA_DIR>/keys.json` is the authentication cornerstone (TenantKeyRegistry — lose it and every tenant's keys are invalidated); `tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}` is the project layout; `tenants/<tid>/.storage/` is the state-isolation boundary; `.mini_cc/` is the SYSTEM-tier plugin dir (`cli.py:88-90`'s `ensure_tier_dir`). See [01](01-overview.md) and [02](02-storage-projects-sessions.md).

**E4. Backend tier** (opensandbox → docker → subprocess). `ServerRuntimeContext._build_runtime()` (`runtime_context.py:57-81`) decides per `MINI_CC_SANDBOX_BACKEND`: `opensandbox` prefers OpenSandboxRuntime, falling through to docker with a warning if env is unconfigured or unavailable (`runtime_context.py:70-73`); `docker` skips straight to DockerRuntime; `auto` (default) tries opensandbox first then docker, returning `None` if neither is available. When `None` is returned, `_sandbox_factory` (`runtime_context.py:111-130`) **auto-degrades** every tenant marked `container` to `SubprocessSandbox`, appending the degrade event to `self.degrades` (`runtime_context.py:117-119`). This soft degrade is intentional — docker being unavailable should not brick the service; it only loses the container-isolation tier. The three tiers + `MountSpec` are covered in [03](03-sandbox.md).

**E5. Single-origin frontend**. Production does not need two origins. `build_app()` (`app.py:210-247`) auto-detects `mini_cc/web/dist` (order: `MINI_CC_WEB_DIST` env → `<cwd>/mini_cc/web/dist` → `<package>/web/dist`), and on finding it mounts `/assets/*` as static files and adds a catch-all GET fallback returning `index.html` for any non-API path — a necessary condition for HashRouter deep links to load directly. Deployment shrinks to three steps: `npm run build` produces `dist/` → `python -m mini_cc.server` auto-mounts → tighten CORS to an explicit allowlist since the browser talks only to FastAPI.

### F. E2E tests

`mini_cc/web/e2e/` holds a Playwright suite; `NOTES.md` maintains the full coverage matrix (21 specs passing). `globalSetup.ts` (`e2e/globalSetup.ts:20-92`) auto-keygens two e2e keys (fine-grained scopes one, `*` for admin one) and creates the e2e project, writing the keys into `process.env.E2E_API_KEY` / `E2E_ADMIN_KEY`; specs read them via `helpers.ts`. Coverage: **W1 CRUD** (`workflow_v2_w1_api.spec.ts`, definition create→get→list→update→delete), **W2 checkpoint** (`workflow_v2_advanced.spec.ts`, `workflow_v2_resolvers.spec.ts`, Approve advances / Reject fails), **W3 webhook_wait**, **W4 email_wait** (`workflow_v2_email.spec.ts`, matching/non-matching/state-error), **W5 validate**, **W6 UI** (`workflow_v2*.spec.ts`, authoring/empty state/polling/def CRUD), **R2 hardening** (`round2_hardening.spec.ts`, cross-tenant isolation), **B8 SSE resume** (`round2_sse*.spec.ts`, `Last-Event-Id` accepted, `resume=true` skips dispatch and replays only, client reconnects on mid-stream drop).

`NOTES.md` also honestly records the UX and functional gaps found and fixed in this round (no "start run" button in empty state, no delete in DefinitionEditor, duplicate LLM dispatch on SSE reconnect), and items not yet covered (action-step real LLM dispatch, IMAP poll loop, batch tests needing fault injection).

---

## Operation & verification

Backend defaults to `:8000`, frontend to `:5173` (this machine's `NOTES.md` actually uses `:8002` and `:5174`).

```bash
# 1. Backend
MINI_CC_DATA_DIR=$PWD/mini_cc_data ANTHROPIC_API_KEY=sk-... python -m mini_cc.server
# Startup JSON line carries host/port/data_dir/sandbox_backend; missing LLM creds → warning "/send will fail with 401"

# 2. Frontend (dev mode, cross-origin)
cd mini_cc/web && npm install && npm run dev

# 3. trace_id end-to-end: response header echoes X-Trace-Id, backend log JSON carries "trace_id"
curl -sS -D - http://127.0.0.1:8002/health -H "X-Trace-Id: my-trace-123" | head

# 4. RedactingFilter: after a /send, grep stdout "mck_" → only [REDACTED:mck_key]

# 5. metrics endpoints
curl -sS http://127.0.0.1:8002/metrics | head      # Prometheus text
curl -sS http://127.0.0.1:8002/metrics.json        # JSON snapshot

# 6. backend tier selection: startup log's sandbox_backend field is the actual runtime class name
MINI_CC_SANDBOX_BACKEND=auto python -m mini_cc.server 2>&1 | head

# 7. single-origin production deployment
cd mini_cc/web && npm run build && cd - && python -m mini_cc.server
# Open http://127.0.0.1:8000/ in a browser → React UI renders directly (from mounted dist/)

# 8. run e2e
cd mini_cc/web && E2E_API_KEY=mck_... npx playwright test --project=chromium
```

Manual Workflow V2 verification: go to `#/projects/<pid>/workflows`, click ＋ new definition to add a few steps (at least one checkpoint); after saving the middle pane's empty state shows a `▶ start new run` button. Once a run starts, polling fires every 2 seconds; the checkpoint step shows the `⏸ paused` glyph plus inline Approve/Reject — clicking either triggers `/drive` and polling shows the advance.

---

## Pitfalls & best practices

1. **`/health` ok but `/send` 401s.** `ok` only means the process is alive; the real readiness signal is `llm_configured` (`app.py:182-183`). Build your readiness probe on that field. Missing credentials at startup trigger an explicit `cmd_serve` warning — read the startup log.

2. **A Workflow V2 run won't advance, stays paused.** Check (a) does a chat session exist — the RunView amber banner should appear; its absence means `sessionsLoaded` hasn't resolved yet; (b) did the drive request actually fire — `drive()` errors explicitly when `sessionId` is empty (`RunView.tsx:100-104`), it does not fail silently.

3. **After an SSE drop the client re-runs the LLM.** This used to happen before the B8 fix; now confirm: the server takes the `body.resume=true` resume-only path, and the client `sse.ts:65-68` only sets `resume=true` and `Last-Event-Id` when `attempt > 0`. If you fork the client, do not default `resume` to true — the initial request must be a normal dispatch.

4. **JSON logs are hard to read.** Set `MINI_CC_LOG_FORMAT=text` for human-readable output (`logging_config.py:178-179`). JSON is still recommended in production: stable, machine-parseable fields.

5. **Changes to `.env` don't take effect.** `main()` calls `load_dotenv()` **before** parsing argv (`cli.py:372-379`), but dotenv only fires if a `.env` exists in cwd or the package directory. If yours lives elsewhere, `cd` there or `export` before starting.

6. **`npm run build` produces dist/ but it isn't mounted.** Detection order (`app.py:212-218`): `MINI_CC_WEB_DIST` → `<cwd>/mini_cc/web/dist` → `<package>/web/dist`. Starting from the repo root with `<repo>/mini_cc/web/dist/index.html` present auto-mounts.

7. **Logs show `docker_available: false` but a tenant is configured for container.** That's the soft degrade — `ServerRuntimeContext` falls every container tenant back to subprocess and records a degrade event (`runtime_context.py:117-119`). Inspect `ctx.degrades` or switch backend.

8. **CORS `*` with cookies / Authorization doesn't work.** `*` and `allow_credentials=true` are mutually exclusive per the browser spec. After single-origin mounting, set `MINI_CC_CORS_ORIGINS` to an explicit allowlist.

---

## Summary

The mini_cc "shell" is assembled from three pieces: the **React web UI** translates SDK capabilities into clickable conversation/workflow/admin surfaces — HashRouter + zustand + a fetch/ReadableStream SSE client form its skeleton; **observability** rests on `TraceIdMiddleware` injecting contextvars, `JsonFormatter` emitting structured lines, `RedactingFilter` scrubbing credentials, `MetricsMiddleware` counting RED metrics, and `tracing.log_span` opt-in OTel — together they let a request crossing middleware/SessionManager/AgentLoop/tools/SSE bridge be stitched end-to-end by a single `trace_id`; **deployment** aims for "single-process full-stack" — `python -m mini_cc.server` brings the backend up in one command, `npm run build` then has `dist/` mounted same-origin by the same FastAPI, with the sandbox backend soft-degrading across opensandbox/docker/subprocess. The three together turn a runnable SDK into a deployable, operable, human-usable product.

---

### Closing the whole series

This is chapter 14 of mini_cc's advanced internals — the final chapter. Across fourteen chapters we have come full circle: starting from the [01 overview](01-overview.md)'s layered architecture and the "no module-level global state" decision, we dissected storage/projects/sessions ([02](02-storage-projects-sessions.md)), three-tier sandbox ([03](03-sandbox.md)), the agent loop and tool dispatch ([04](04-agent-loop.md)), the tools toolbox ([05](05-tools.md)), the permissions model ([06](06-permissions.md)), HTTP server ([07](07-http-server.md)), SSE streaming and resume ([08](08-sse-streaming.md)), auth, authorization and tenant isolation ([09](09-auth.md)), Workflow V2 ([10](10-workflow-v2.md)), MCP client and three-tier plugins ([11](11-mcp-plugins.md)), skills/slash/LSP ([12](12-skills-commands-lsp.md)), agent teams and three async primitives ([13](13-teams-scheduler.md)), and finally here — web UI, end-to-end observability, deployment shape. This sweep covers every subsystem: from the lowest-level Protocol+multi-implementation extension points out to the browser-facing UI and operator runbook. Read in order, you should now be able to explain both the "why this design" and the "where it can be extended" of every mini_cc link; if you jumped around, head back to the [01](01-overview.md) overview map to locate the subsystem you care about and dive in. The value of mini_cc is not in how many features it implements but in how those features compose into a clearly-layered, embeddable, observable, extensible framework — we hope this tutorial has handed you that "complete map."
