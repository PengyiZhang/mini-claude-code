# 06 — Permissions & Scopes

[ < prev ] [ next > ] · [中文版本](../zh/06-permissions.md)

## Problem & motivation

A backend-integrable agent framework runs untrusted model output (tool calls)
against real filesystems and shells, and serves multiple tenants over HTTP. That
introduces two distinct authorization surfaces that must not be conflated:

1. **Tool-level authorization** — should *this* invocation of `bash` / `write_file`
   proceed, or must a human approve it first? This is per-session, per-tool, and
   inherently interactive (the model is mid-turn, waiting).
2. **HTTP-level authorization** — should *this* API key be allowed to call
   *this* route? This is per-tenant, per-route, and stateless (a request comes
   in, a decision is made, the request proceeds or 401/403s).

`mini_cc` splits these cleanly. Tool-level prompts live in
`mini_cc/core/permissions.py` (interactive `PermissionInterceptor`) plus a
non-interactive `make_permission_hook` default-deny hook for deployments that
don't want prompts. HTTP-level scopes live in `mini_cc/auth/scope.py` and are
enforced by `require_scope` in the FastAPI dependency layer. Per-project config
rides in `<workspace>/.mini_cc/permissions.toml`. This chapter walks each layer
and how they compose.

## Design & principles

### Layer 1: interactive tool prompts (PermissionInterceptor)

When a project opts in via `permissions.toml`, the loop intercepts tool calls
matching the configured `prompt_tools` set and blocks until a human decides.
The flow lives in `AgentLoop._execute_tool_calls`
(`mini_cc/core/loop.py:839-868`):

```
   model calls bash (and bash ∈ prompt_tools)
            │
            ▼
   interceptor.create(session_id, "bash", input)
            │   ── returns PermissionRequest with uuid4 hex id
            ▼
   loop yields {"type":"permission_request", request_id, ...}
            │   ── SSE client renders a prompt UI
            ▼
   interceptor.wait(request_id, stop_event)
            │   ── blocks on a threading.Event, polling every 1s
            │      so session stop / cancel can interrupt the wait
            ▼
   one of three outcomes:
     • decide(allow)  → fall through to normal tool execution
     • decide(deny)   → tool_result = deny_message
     • timeout/cancel → tool_result = "[permission timed out]"
                       or "[cancelled by session stop]"
```

The interceptor is **per-project** (one instance serves many sessions). Pending
requests live in a dict keyed by `request_id`; `list_pending(session_id)`
filters for the session so a UI can show only its own prompts. The waiter pops
the request from the registry on exit, so decided requests don't linger
(`mini_cc/core/permissions.py:114-119`).

The `wait()` loop is deliberately poll-based (1s default) rather than a single
blocking `Event.wait()` — that way `stop_event.is_set()` (set when the user
hits stop or the SSE worker bails) interrupts the wait within the poll
interval, instead of stranding the loop until the full timeout elapses.

### Layer 2: non-interactive default-deny hook

For deployments that don't want interactive prompts (headless servers,
automated pipelines), `make_permission_hook`
(`mini_cc/core/hooks.py:82-105`) returns a `PreToolUse` hook that denies
dangerous bash commands outright. It has two tiers:

```python
# mini_cc/core/hooks.py:78-79
DENY_LIST = ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")
DESTRUCTIVE = ("rm ", "> /etc/", "chmod 777")
```

`DENY_LIST` is always blocked; `DESTRUCTIVE` is blocked unless
`allow_destructive=True`. The denial string becomes the tool's `tool_result`
content, so the model sees *why* it was blocked and can try a safer command.
This is defense-in-depth on top of the sandbox policy — applications can
register their own hook with arbitrary Python logic for finer control.

Key distinction: a `PreToolUse` hook returning a non-None string **denies** the
call; the loop short-circuits and yields that string as the result
(`loop.py:873-876`). The interactive interceptor is checked *before* the hook,
so a human-approved call still runs even if a hook would have denied it
automatically.

### The Hooks registry itself

`Hooks` (`mini_cc/core/hooks.py:30`) is a per-project event registry with four
events: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`. Each callback
is wrapped in try/except (B3) so a buggy hook logs and is skipped rather than
taking down the turn. `trigger()` returns the first non-None result — that's
how `PreToolUse` denials propagate. Convenience builders:
`make_log_hook` (audit every call), `make_large_output_hook` (signal when
output exceeds a threshold), `make_audit_hook` (pre-wired registry forwarding
all events to a single sink).

### Layer 3: per-project config (permissions.toml)

`load_permissions_config(workspace)` (`mini_cc/projects/permissions_config.py:33`)
reads `<workspace>/.mini_cc/permissions.toml`:

```toml
prompt_tools = ["bash", "write_file", "edit_file"]
timeout_seconds = 300   # optional; default 300
```

Returns `None` when the file is missing (interactive prompts disabled — today's
default) or malformed. Unknown tool names in `prompt_tools` are silently
ignored so the file is forward-compatible. The project manager wires this up
at construction (`mini_cc/projects/manager.py:292-295`): if `perm_cfg` is None,
`project.permissions` is None and `prompt_tools` is an empty set, so the loop's
interceptor check (`loop.py:840`) short-circuits and no prompts ever fire.

### Layer 4: HTTP scope authorization

API keys carry a `scopes` list (`mini_cc/auth/keys.py:53-61`, default
`["*"]`). The grammar is colon-joined `resource:verb`
(`mini_cc/auth/scope.py:1-15`):

| Scope | Matches |
|-------|---------|
| `*` | anything |
| `read:*` | any GET |
| `write:*` | any non-GET |
| `sessions:*` | any method on `/sessions/...` |
| `sessions:read` | GET on `/sessions/...` |
| `sessions:write` | POST/DELETE/PUT/PATCH on `/sessions/...` |
| `sessions` | shorthand for `sessions:*` |

`read` ≡ GET, `write` ≡ everything else. The mapping is fixed at the HTTP layer
so routes don't specify the verb explicitly. `scope_allows(held, required,
method)` (`scope.py:56`) resolves both sides and returns True if any held
scope satisfies the required one for the request's method.

Enforcement happens in `require_scope(required)` (`mini_cc/server/deps.py:56`),
a FastAPI dependency factory. The `_resolve` helper (line 73) parses the bearer
token, looks up the `KeyRecord`, enforces tenant match, then calls
`scope_allows`. On failure it raises 401 (missing/unknown/expired key) or 403
(tenant mismatch or insufficient scope). The 403 for insufficient scope
includes `WWW-Authenticate: Bearer scope="..."` and an `insufficient_scope`
detail code so clients can introspect (`deps.py:84-91`).

The permission routes themselves are scope-gated:
`mini_cc/server/routes/permissions.py:41` lists pending (`sessions:read`) and
line 56 decides (`sessions:write`).

## Operation & configuration

### Enabling interactive tool prompts

Drop a file at `<workspace>/.mini_cc/permissions.toml`:

```toml
# Only these tools trigger a prompt; everything else runs automatically.
prompt_tools = ["bash", "write_file", "edit_file", "execute_code"]
timeout_seconds = 300
```

Restart the project (or rebuild it) — the config is read once at construction.
Unknown tool names are ignored; a typo in `prompt_tools` silently disables the
prompt for that tool.

### Enabling the non-interactive deny hook

```python
from mini_cc.core.hooks import Hooks, make_permission_hook

hooks = Hooks()
hooks.register(Hooks.PreToolUse, make_permission_hook(allow_destructive=False))
# Pass `hooks=hooks` when constructing the project / AgentLoop.
```

For audit-only tracing without blocking, add `make_log_hook(sink)` to the same
registry.

### HTTP scope management

Scopes are assigned at key creation (`mini_cc/auth/keys.py:97`,
`generate(tenant_id, scopes=[...])`). A key with `scopes=["read:*"]` can GET
anything but cannot POST/DELETE. The `*` wildcard (default) grants full access.
Rotated keys inherit the prior key's scopes (line 175).

### Permission HTTP endpoints

Base path: `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions`

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| GET | `` | `sessions:read` | List pending requests for this session |
| POST | `/{req_id}/decide` | `sessions:write` | Allow/deny a specific request |

Both return empty/404 when no `permissions.toml` is configured (interceptor is
None). The decide body is `{"decision": "allow"|"deny", "message": "..."}`.

## Verification steps

```bash
# 1. Without permissions.toml: no prompts, decide returns 404
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision":"allow"}' \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions/abc/decide"
# → 404

# 2. Create <workspace>/.mini_cc/permissions.toml, rebuild project, then:
#    ask the agent to run bash; a permission_request event appears in SSE.
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"Run: ls -la"}'
# Watch for {"type":"permission_request","request_id":"...","tool_name":"bash",...}

# 3. List pending, then allow it
curl -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions"
# → [{"request_id":"<id>",...}]

curl -X POST -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision":"allow"}' \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions/<id>/decide"
# → 204; the blocked bash call now proceeds

# 4. Scope enforcement: create a read-only key, try to POST
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $READONLY_KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"hi"}'
# → 403 with insufficient_scope detail
```

## Common pitfalls & debugging

1. **Prompts never fire** — the interceptor is None because
   `permissions.toml` is missing, malformed, or in the wrong workspace. The
   file must be at `<workspace>/.mini_cc/permissions.toml`, read once at
   project construction; restart to pick up changes.
2. **`permission_request` appears but decide returns 404** — the request was
   already popped (decided, timed out, or cancelled). `wait()` removes the
   entry on exit, so a slow UI that polls list_pending then decides can race
   the timeout. Use the `request_id` from the live SSE event, not a stale
   list.
3. **Prompt blocks forever** — check `timeout_seconds` (default 300). If a
   client never connects to the SSE stream, the loop waits the full duration
   before yielding a `[permission timed out]` tool_result. Lower the timeout
   for automated environments, or don't configure `prompt_tools` at all.
4. **`make_permission_hook` denies but model keeps retrying** — the denial
   string is the model's only signal. Make it actionable: "Permission denied:
   destructive command (matched 'rm '); override with allow_destructive=True"
   tells the model *what* to change, not just that it failed.
5. **403 with `insufficient_scope` on a `*` key** — the key's tenant doesn't
   match the path tenant (`rec.tenant_id != tid` raises the same 403). Check
   the `WWW-Authenticate` header and the `held` list in the error body to
   distinguish scope mismatch from tenant mismatch.

## Further reading

- Source: `mini_cc/core/permissions.py`, `mini_cc/core/hooks.py`,
  `mini_cc/projects/permissions_config.py`, `mini_cc/auth/scope.py`,
  `mini_cc/auth/keys.py`, `mini_cc/server/deps.py`,
  `mini_cc/server/routes/permissions.py`
- Sibling chapters: [04 — Agent Loop](../en/04-agent-loop.md),
  [05 — Tools](../en/05-tools.md)
- For the sandbox policy layer (the third defense-in-depth tier below the
  deny-list hook), see `mini_cc/sandbox/` and the `sNN-*` conceptual series.
