[ < [06](../zh/06-placeholder.md) ] [ [08](08-sse-streaming.md) > ] · [中文版本](../zh/07-http-server.md)

# 07 — HTTP Server & Routing

> `build_app` is a pure factory: it takes an already-wired SDK
> (`ProjectManager`, `SessionManager`, `TenantKeyRegistry`) and returns a
> FastAPI app. The server owns no business state — it is a thin transport.
> This chapter covers the lifespan contract, the middleware stack order,
> the tenant-boundary enforcement pattern shared by every route, the
> `MiniCCError` envelope, and the per-tenant token-bucket rate limiter.

---

## Problem & motivation

s20 ships a Python SDK (`SessionManager.send()` returns a sync iterator
of events). That shape is great inside a Python process and useless to a
browser, a curl user, or a backend integration in another language. The
HTTP/SSE transport layer (Phase A → Phase E of the server initiative)
exists to expose that SDK safely to multi-tenant external callers.

"Safe" unpacks into four hard requirements that drive every design
choice in `mini_cc/server/`:

1. **Tenant isolation**. Two tenants sharing one process must never read
   each other's projects, sessions, or transcripts — even if a developer
   fat-fingers a route handler and forgets a check. The check has to be
   *mechanical and uniform*, not "remembered per route".
2. **Uniform error shape**. Every failure path — 404, 409, 429, an SDK
   `KeyError` — must render as the same JSON envelope so clients can
   write one error handler.
3. **Observable**. Every request needs a trace id (so logs stitch
   together) and RED metrics (Rate / Errors / Duration) tagged by tenant
   and route template.
4. **Graceful teardown**. Killing the process mid-LLM-call leaves
   Anthropic client threads hung and MCP stdio subprocesses leaked.
   Lifespan shutdown has to stop sessions, join teammates, disconnect
   MCP pools, and tear down containers — in that order.

The factory + lifespan + middleware + dependency pattern in
`mini_cc/server/app.py` is the answer to all four.

---

## Design & principles

### Factory, not module global

`build_app` (`mini_cc/server/app.py:98`) constructs the FastAPI app from
explicitly-passed dependencies. Nothing at module import time touches
the filesystem or binds a port. Tests construct one app per scenario
with stub managers; production constructs exactly one via
`cmd_serve` (`mini_cc/server/cli.py:114`).

```
        cmd_serve (cli.py)
              │
              ▼
   ServerRuntimeContext ──► build_project_manager() ──► ProjectManager
        │                                                │
        │                                                ▼
        │                                          SessionManager
        ▼
   TenantKeyRegistry ──┐
   TenantRateLimiter ──┼──► build_app(data_dir, key_registry, pm, sm,
   MetricsRegistry   ──┘         cors_origins, rate_limiter,
                                 metrics_registry, server_runtime)
                                         │
                                         ▼
                                    FastAPI
```

Every manager ends up on `app.state`, where dependency-injection helpers
(`get_pm`, `get_sm`, `get_registry` in `deps.py`) fish it back out.

### Lifespan: ordered teardown

`lifespan` (`app.py:57`) yields once at startup (after warning about
insecure default secrets), then on shutdown runs four cleanup passes in
a deliberately chosen order:

1. **Stop every live session** (`sess.stop()`). Sets the loop's cancel
   flag so an in-flight Anthropic call returns instead of hanging.
2. **Shut down teammate spawners** (`spawner.shutdown(timeout=5.0)`).
   Each TeammateSpawner sends a shutdown_request through the bus and
   joins worker threads so mailbox writes aren't mid-flight.
3. **Disconnect MCP pools** (`pool.disconnect_all()`). Without this,
   stdio MCP subprocesses and HTTP connection pools leak across
   restarts.
4. **Stop per-tenant containers** (`ctx.shutdown()`). Calls
   `TenantContainerManager.stop()` for every cached tenant.

Each pass is wrapped in `try/except` — one dirty session must not abort
the rest of teardown.

### Middleware stack: order matters

```
   request  ──►  TraceIdMiddleware      (outermost: assigns X-Trace-Id)
                ─► MetricsMiddleware    (RED metrics, ASGI-native)
                   ─► CORSMiddleware    (allow Last-Event-Id header)
                      ─► router
```

`TraceIdMiddleware` (`middleware.py:25`) is added first so it wraps
everything below — even a CORS rejection gets a trace id in its log
line. `MetricsMiddleware` (`middleware.py:46`) is ASGI-native (not
`BaseHTTPMiddleware`) precisely so it can read `scope["route"]` to tag
metrics with the route template like `/tenants/{tid}/projects/{pid}/...`
rather than the raw path. CORS exposes `Last-Event-Id` and `X-Trace-Id`
(`app.py:136`) — the former is mandatory for SSE resume (ch 08).

### The tenant-boundary enforcement pattern

Every protected route has the same shape, and the boundary check is
mechanical — a route that forgets it cannot compile past review:

```python
# mini_cc/server/routes/sessions.py:31 (simplified)
@router.post("", response_model=SessionOut)
def start_session(body: CreateSessionRequest,
                  tid: str = Depends(require_scope("sessions:write")),  # 1
                  pm=Depends(get_pm), sm=Depends(get_sm)) -> SessionOut:
    validate_id(pid)                          # 2 — defense-in-depth ID check
    _check_project_tenant(pid, tid, pm)       # 3 — project exists + belongs to tid
    ...
```

Three layers, each catching a different class of bug:

1. **`require_scope(required)`** (`deps.py:56`) parses the bearer token,
   looks it up in the registry, verifies the key's `tenant_id == tid`
   from the path, and checks the key's scopes cover the required one.
   Mismatch → 403, missing/expired → 401.
2. **`validate_id`** (`deps.py:98`) rejects any path ID that isn't
   `[A-Za-z0-9_-]+` — kills path-traversal via crafted `project_id`
   values before the SDK ever sees them.
3. **`_check_project_tenant`** (`sessions.py:20`) re-fetches the project
   and asserts `p.meta.tenant_id == tid`. Even if a caller guesses
   another tenant's `pid`, they get 404 — never a leak. Same pattern in
   `workflow_v2.py:_service_for` and the `webhooks` router.

### The `MiniCCError` envelope

All exceptions raised inside routes inherit `MiniCCError`
(`errors.py:13`). Two exception handlers in `build_app`
(`app.py:142`, `app.py:155`) render every one of them through the same
JSON shape:

```json
{"error": {"code": "rate_limited", "message": "...", "details": {...}}}
```

`map_sdk_exception` (`errors.py:63`) translates raw SDK exceptions —
`KeyError` → 404, `ValueError("already exists")` → 409, other
`ValueError` → 400 — so route handlers can `raise map_sdk_exception(e)`
and let the envelope machinery do the rest. The 429 handler additionally
emits `Retry-After` from `request.state.rate_limit_retry_after`.

### Per-tenant token-bucket rate limiter

`TenantRateLimiter` (`ratelimit.py:45`) is a thread-safe in-memory
token bucket keyed by tenant. Default 60 RPM, capacity = RPM (so a
tenant can burst a full minute's quota at once), refill rate = RPM/60
tokens/sec. The bucket is created lazily on first request; per-tenant
overrides come from `MINI_CC_RATE_LIMIT_RPM=tenant=rpm,...`.

`_apply_rate_limit` (`deps.py:143`) is wired in via
`check_rate_limit_scope(required)` — the same dependency that does
scope enforcement also consumes one token. That composition is the
reason routes declare one `Depends(...)`, not two.

For multi-process deployments `make_rate_limiter` (`ratelimit.py:144`)
prefers `RedisRateLimiter` (fixed 60s window per tenant, INCR + TTL).
If Redis is unreachable at construction time it falls back to in-memory
and logs a warning — the server still boots, multi-process deployments
just lose shared counters.

---

## Operation & configuration

### Environment variables (read in `cli.py` unless noted)

| Env var | Default | Meaning | Source |
|---|---|---|---|
| `MINI_CC_HOST` | `127.0.0.1` | Bind host | `cli.py:118` |
| `MINI_CC_PORT` | `8000` | Bind port | `cli.py:119` |
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | Project + keys.json root | `cli.py:23` |
| `MINI_CC_CORS_ORIGINS` | `*` | Comma-separated allowed origins | `cli.py:111` |
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` | `60` | Default per-tenant RPM | `cli.py:36` |
| `MINI_CC_RATE_LIMIT_RPM` | (empty) | `tenant=rpm,tenant=rpm` overrides | `cli.py:38` |
| `MINI_CC_LOG_FORMAT` | `json` | `json` or `text` | `cli.py:69` |
| `MINI_CC_LOG_LEVEL` | `INFO` | Logging level | `cli.py:70` |
| `MINI_CC_WEB_DIST` | (empty) | Override path to built React UI | `app.py:210` |

### CLI subcommands (`python -m mini_cc.server ...`)

| Subcommand | Effect |
|---|---|
| `serve` (or no args) | Run uvicorn on `HOST:PORT` |
| `keygen <tid> [--scopes S] [--expires-in 7d] [--label L]` | Mint an API key |
| `keys list <tid>` | List keys (incl. expired) |
| `keys rotate <key> [--grace-hours N]` | Rotate, optionally graceful |
| `revoke <key>` | Hard-revoke a key |
| `sandbox status [--tid T]` / `stop T` / `build-image` | Container lifecycle |

### Meta endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `{ok, llm_configured}` — readiness probe |
| `GET /metrics` | none (trusted-network) | Prometheus 0.0.4 text |
| `GET /metrics.json` | none (trusted-network) | JSON snapshot |
| `GET /tenants/{tid}/admin/metrics.json` | `admin:read` | Per-tenant metrics |

### Route groups registered in `build_app`

`projects`, `sessions`, `resources` (+ `download_router`), `permissions`,
`admin`, `commands`, `run_table`, `share_router` (sessions), `webhooks`,
and `workflow_v2.ALL_ROUTERS` (`definitions_router`, `runs_router`).
The SPA catch-all (`/{path:path}` GET) is added last so it never shadows
a real API route.

---

## Verification steps

Run against the backend on `:8002` (substitute your tenant/key).

```bash
# 1. Readiness — llm_configured must be true before /send works.
curl -s http://127.0.0.1:8002/health
# {"ok":true,"llm_configured":true}

# 2. Tenant boundary: cross-tenant pid returns 404, NOT a leak.
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $KEY_TENANT_A" \
  http://127.0.0.1:8002/tenants/tenantA/projects/tenantB_pid/sessions
# 404

# 3. Missing bearer → 401 in the standard envelope.
curl -s http://127.0.0.1:8002/tenants/tenantA/projects | jq .
# {"error":{"code":"unauthorized","message":"missing bearer token","details":{}}}

# 4. Insufficient scope → 403 with WWW-Authenticate.
curl -s -i -H "Authorization: Bearer $READ_ONLY_KEY" \
  -X POST http://127.0.0.1:8002/tenants/tenantA/projects \
  -H 'Content-Type: application/json' -d '{"project_id":"p1"}' \
  | grep -i 'www-authenticate'
# WWW-Authenticate: Bearer scope="projects:write"

# 5. Rate limit: hammer until 429, observe Retry-After.
for i in $(seq 1 70); do
  curl -s -o /dev/null -w "%{http_code} " \
    -H "Authorization: Bearer $KEY" \
    http://127.0.0.1:8002/tenants/tenantA/projects
done
# ... 200 200 200 429 429 ...

# 6. Metrics carry the route template + tenant tag.
curl -s http://127.0.0.1:8002/metrics.json | jq '.counters.http_requests_total'
```

---

## Common pitfalls / debugging

1. **"My route returns 404 for a project I know exists"**. Check that
   the bearer key's `tenant_id` matches `{tid}` *and* that the project's
   `meta.tenant_id` matches too. Both checks fail closed to 404 to avoid
   existence leaks — `Forbidden("api key does not match tenant")` only
   fires when the path tid differs from the key's tid, not when the
   project belongs to a different tenant.
2. **"429 even though I'm under 60 RPM"**. The bucket capacity equals
   RPM, so 60 simultaneous requests consume the whole minute at once
   and the 61st is denied even within the first second. Either raise
   `MINI_CC_RATE_LIMIT_RPM_DEFAULT`, or pace your client.
3. **Forgetting `validate_id`**. The regex check in `deps.py:24` is the
   only thing standing between a crafted `../../../etc/passwd` style
   `project_id` and the storage layer. Every route that takes a path ID
   must call it — there is no global pre-route hook.
4. **Middleware order bugs**. If you add a new middleware *after*
   `MetricsMiddleware`, metrics for that layer's rejections won't carry
   a trace id. `TraceIdMiddleware` must be added first; see the comment
   at `app.py:124`.
5. **Redis rate limiter fails open... sort of**. `RedisRateLimiter.allow`
   raises `RuntimeError` on Redis unavailability (`ratelimit.py:136`),
   *not* silently fail-open. That's deliberate — but `make_rate_limiter`
   only probes at construction; a Redis that goes down mid-process will
   start raising at request time. Monitor `ratelimit.redis_unavailable`
   log lines.

---

## Further reading

- Source: `mini_cc/server/app.py`, `mini_cc/server/deps.py`,
  `mini_cc/server/errors.py`, `mini_cc/server/ratelimit.py`,
  `mini_cc/server/middleware.py`, `mini_cc/server/runtime_context.py`
- Sibling: [08 — SSE Streaming & Resume](08-sse-streaming.md),
  [09 — Auth, Scopes, Share Tokens](09-auth.md)
- Conceptual: `../../en/sNN-*.md` (see chapter index)
