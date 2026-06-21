# Phase F: Web UI (keys + metrics + permissions + cold/warm sessions)

## Context

Phases A–E shipped (383 tests). The backend now exposes several
production-grade capabilities that the Web UI hasn't picked up:

- **Phase D** keys API — scopes, expiry, rotation. CLI only.
- **Phase E** `/metrics` + `/metrics.json` — global Prometheus snapshot.
- **Phase C** `/permissions` GET + `POST /decide` — interactive prompts.
- **Phase B** session list with `in_memory` flag — cold/warm distinction.

The existing React UI (P4, shipped at `475ac17`) handles chat, file
management, and basic session lifecycle but doesn't surface any of
these. Phase F closes the loop.

## Approach (locked via brainstorm)

- **Scope**: full — Keys + Metrics + Permissions + cold/warm indicators.
- **Auth model**: separate "admin" login gate for the Keys page (and
  Metrics dashboard). Requires a `*`-scope (admin) key. Chat-side auth
  unchanged.
- **Keys routes**: add `/tenants/{tid}/admin/keys*` (list =
  `admin:read`, mutations = `admin:write`). Revoke and rotate under
  the same admin:write gate.
- **Metrics**: keep global `/metrics.json` no-auth (Prometheus
  scraper). Add tenant-filtered
  `/tenants/{tid}/admin/metrics.json` (admin:read) for the dashboard.
- **Permission prompts**: render inline on the `permission_request`
  SSE event with a live countdown. On chat-tab mount, GET
  `/permissions` once to recover. Poll every 5s while chat tab is
  visible but session not streaming.
- **Cold/warm session indicator**: green/grey dot from the existing
  `in_memory` field. Cold rows get a `[Warm up]` button calling
  `POST /sessions/{sid}/resume`.

## File-by-file

### `mini_cc/server/routes/admin.py` (new, ~140 lines)

```python
router = APIRouter(prefix="/tenants/{tid}/admin",
                   tags=["admin"])

@router.get("/keys", response_model=list[KeyOut])
def list_keys(tid: str = Depends(require_scope("admin:read")),
              reg=Depends(get_registry)) -> list[KeyOut]: ...

@router.post("/keys", response_model=KeyOut, status_code=201)
def create_key(body: CreateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> KeyOut: ...

@router.patch("/keys/{key}", response_model=KeyOut)
def update_key(key: str, body: UpdateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> KeyOut: ...

@router.delete("/keys/{key}", status_code=204)
def revoke_key(key: str,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> None: ...

@router.post("/keys/{key}/rotate", response_model=RotateKeyOut)
def rotate_key(key: str, body: RotateKeyRequest,
               tid: str = Depends(require_scope("admin:write")),
               reg=Depends(get_registry)) -> RotateKeyOut: ...

@router.get("/metrics.json")
def tenant_metrics(tid: str = Depends(require_scope("admin:read")),
                   request: Request) -> dict:
    reg: MetricsRegistry = request.app.state.metrics
    return reg.snapshot_for_tenant(tid)
```

`list_keys` filters by tenant match. `revoke`/`update`/`rotate`
verify the resolved key's tenant_id matches `{tid}` before mutating
(same defense-in-depth as the existing routes).

### `mini_cc/server/schemas.py` (modify)

```python
class KeyOut(BaseModel):
    key: str
    tenant_id: str
    scopes: list[str]
    created_at: str
    expires_at: str | None
    label: str
    rotated_from: str | None

class CreateKeyRequest(BaseModel):
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str = ""

class UpdateKeyRequest(BaseModel):
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str | None = None

class RotateKeyRequest(BaseModel):
    grace_hours: int = 0
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str | None = None

class RotateKeyOut(BaseModel):
    new_key: KeyOut
    old_key: KeyOut | None  # None if hard-revoked
```

### `mini_cc/server/metrics.py` (modify)

Add `snapshot_for_tenant(tenant)`:

```python
def snapshot_for_tenant(self, tenant: str) -> dict:
    snap = self.snapshot()
    # Filter each counter / histogram's series by tenant label.
    for family in snap["counters"].values():
        if "tenant" in family["label_names"]:
            family["series"] = [
                s for s in family["series"]
                if s["labels"].get("tenant") == tenant]
    for family in snap["histograms"].values():
        if "tenant" in family["label_names"]:
            family["series"] = [
                s for s in family["series"]
                if s["labels"].get("tenant") == tenant]
    return snap
```

### `mini_cc/server/app.py` (modify)

```python
app.include_router(admin_routes.router)
```

### `tests/test_phaseF_admin_http.py` (new)

- admin:read key → list_keys 200; admin:write key → list_keys 403.
- POST /admin/keys creates a KeyRecord on disk with the given
  scopes/expiry.
- PATCH updates scopes/label.
- DELETE removes from registry.
- POST /rotate returns new+old; old still in list when grace>0.
- /admin/metrics.json filters out other tenants' series.
- Cross-tenant access: admin key for tenant1 cannot mutate tenant2's
  keys (404 / 403).

### Web UI new files

#### `mini_cc/web/src/pages/AdminLogin.tsx`

Form accepts `{tenant_id, key}`. On submit, probe
`GET /tenants/{tid}/admin/keys` with the key. 200 → store in
`useAdmin`, redirect to `/admin/keys`. 401/403 → show "not an admin
key" error.

#### `mini_cc/web/src/pages/AdminKeys.tsx`

Table of keys for the logged-in admin tenant. Columns:
- Truncated `mck_…` (full reveal only on creation/rotation)
- Scopes as colored chips (`*` purple, `read:*`/`write:*` blue,
  resource-scoped orange)
- Expiry: ISO date or `never`, greyed "EXPIRED" badge for past dates
- Label
- Actions: `[Patch]` `[Rotate]` `[Revoke]` (disabled for expired)

Modals:
- **New key** — scope multi-select (`*`, `read:*`, `write:*`,
  `sessions:*`, `files:*`, custom input), expiry dropdown (`1h`/`1d`/
  `7d`/`30d`/`never`), label input. On success, modal shows the full
  key once with a copy button.
- **Patch** — inline edit on the same fields.
- **Rotate** — grace-hours slider (0–72), optional new scopes/label.
  On success, modal shows new key + old key status (grace end or
  hard-revoked).
- **Revoke** — confirm dialog → DELETE.

#### `mini_cc/web/src/pages/AdminMetrics.tsx`

- **Refresh toggle**: every 5s (default on) or manual.
- **Ring buffer**: 60 most recent samples in component state.
- **Top cards**:
  - Total requests = sum of `http_requests_total` matching tenant.
  - Total errors = same with `status` starting with `4`/`5`.
  - Total tokens (output) = sum of
    `anthropic_tokens_total{kind="output"}`.
  - In-flight = current `http_in_flight_requests` value (global; not
    tenant-specific — labeled clearly).
- **Sparkline**: SVG path of request count deltas across the ring
  buffer.
- **Latency histogram**: bar chart of
  `http_request_duration_seconds` buckets, with computed p50/p90/p99.
- **Token breakdown**: horizontal bars for input / output / cache_read
  / cache_create totals.

#### `mini_cc/web/src/components/PermissionPrompt.tsx`

Inline card rendered in the chat message log:

```
🔒 Tool: bash
  {"command": "rm -rf build/", ...}

  Requested at 14:23:05
  Auto-deny in: [====    ] 4:23 / 5:00

  [Deny]                    [Allow]
```

- Props: `requestId`, `toolName`, `toolInput`, `createdAt`,
  `timeoutSeconds`.
- Countdown tick: `useEffect` with `setInterval(1000)` computing
  remaining = `createdAt + timeoutSeconds - now`.
- Buttons → `POST /sessions/{sid}/permissions/{req_id}/decide`
  `{decision: "allow"|"deny"}`. On response, the card flips to
  resolved state ("Allowed" / "Denied" / "Timed out").

#### `mini_cc/web/src/components/SessionRow.tsx`

```tsx
<div className="flex items-center gap-3">
  <span className={warm ? "bg-ok" : "bg-border"} />  {/* dot */}
  <div>
    <div>{sessionId}</div>
    <div className="text-sm text-muted">{messages} messages</div>
  </div>
  {warm
    ? null
    : <button onClick={onWarmUp}>Warm up</button>}
</div>
```

`onWarmUp` calls `POST /sessions/{sid}/resume`, then refreshes the
list.

#### `mini_cc/web/src/components/Sparkline.tsx`

Pure SVG, ~30 lines. Takes `values: number[]` and renders a
normalized polyline. No chart library dep.

#### `mini_cc/web/src/lib/adminStore.ts`

```ts
interface AdminState {
  adminKey: string | null
  adminTid: string | null
  keys: KeyOut[]
  metricsHistory: MetricsSnapshot[]  // ring buffer, 60
  login(tid, key): Promise<boolean>
  logout(): void
  refreshKeys(): Promise<void>
  refreshMetrics(): Promise<void>
  // CRUD wrappers
}
```

Separate from `useAuth` so chat-side and admin-side auth don't
collide.

### Web UI modified

- `mini_cc/web/src/lib/api.ts` — add admin + permissions + resume
  endpoints. Accept optional `adminKey` parameter so admin calls
  don't disturb chat-side auth.
- `mini_cc/web/src/lib/types.ts` — `KeyOut`, `MetricsSnapshot`,
  extend `PermissionRequestOut` if needed.
- `mini_cc/web/src/App.tsx` — new routes `/admin/login`,
  `/admin/keys`, `/admin/metrics`. AdminTopBar mounted when
  `useAdmin.adminKey` is set.
- `mini_cc/web/src/components/TopBar.tsx` — "Admin" link when
  `useAdmin.adminKey` set.
- `mini_cc/web/src/pages/Workspace.tsx`:
  - Chat tab: when SSE emits `permission_request`, push a
    `PermissionPrompt` into the message log.
  - On chat tab mount: `GET /sessions/{sid}/permissions` once,
    render any pending as `PermissionPrompt`.
  - Sessions tab: replace plain rows with `SessionRow` showing
    cold/warm dot + Warm up button.

### Playwright e2e

`tests/web/e2e/admin.spec.ts` (new):
- Admin login flow with a `*`-scope key seeded by globalSetup.
- Keys page: list existing keys; create one with `read:*` and 7d
  expiry; rotate it with grace-hours=24; verify old+new both visible.
- Metrics page: load renders cards without errors.
- Permission prompt: trigger a bash command (prompt opted-in),
  watch the inline card appear, click Allow, verify the tool ran.
- Cold/warm: stop server, restart (session goes cold), see grey dot,
  click Warm up, see green dot.

## Verification

```bash
# Backend
python -m pytest tests/test_phaseF_*.py -v
python -m pytest tests/ -q   # expect ~420 passing

# Frontend unit (if any added)
cd mini_cc/web && npm run build

# E2E
cd mini_cc/web && npx playwright test admin.spec.ts

# Manual
python -m mini_cc.server &
cd mini_cc/web && npm run dev
# Chat login → drive a session
# /admin/login with `*`-scope key → manage keys, watch metrics
# Trigger a permission_prompt (bash with permissions.toml enabled)
# Stop server, restart, see cold session, click Warm up
```

## Out of scope for Phase F

- Long-term metric retention (Prometheus / VictoriaMetrics does this).
- Real WebSocket transport (still SSE-only).
- Multi-tenant admin superuser view (one tenant at a time only).
- Bulk key operations (CSV import/export, etc).
- Dark/light theme toggle (dark-only for now).
- i18n (English + Chinese strings hardcoded side by side).
