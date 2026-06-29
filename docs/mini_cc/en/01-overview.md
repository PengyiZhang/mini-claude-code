[ < prev ] [ **02** > ] · [中文版本](../zh/01-overview.md)

# 01 — Overview & Architecture

> This series is the "advanced internals" tier for mini_cc. We assume you
> have already read the `docs/en/s01-s12` conceptual chapters and know the
> Claude Code basics: AgentLoop, tool_use, context compaction. This chapter
> takes you from "can use" to "can modify": it explains mini_cc's layering,
> dependency direction, and where the "one Protocol + multiple impls"
> extension points live.

---

## Problem & motivation

`s20_comprehensive/code.py` is a 2123-LOC single-file teaching reference
that crams 19 subsystems into one `agent_loop()`. It's great for learning
concepts, but you can't ship it as a production backend, for three reasons:

1. **No tenant concept.** When one process serves multiple users, their
   workspaces, messages, and memory bleed into each other. s20 keeps every
   piece of state under cwd; whoever `cd`s first mutates the global.
2. **Not backend-integrable.** `agent_loop()` is a synchronous blocking
   function. To serve it over the network you have to wrap your own
   transport, auth, and concurrency — and every team re-invents that layer.
3. **Module-level global state.** s20's storage, scheduler, mcp_pool are
   module-level singletons. You can't run two independent projects in one
   process.

mini_cc's goal: **keep all of s20's capability, but restructure it into a
multi-tenant, backend-embeddable framework**. You can `import mini_cc` as
an SDK, or `python -m mini_cc.server` to stand up an HTTP/SSE service that
any language can drive. The keystone decision is "**no module-level global
state**" — every subsystem instance is per-project, and
`ProjectManager._assemble()` wires all of a project's dependencies in one
shot (`mini_cc/projects/manager.py:278`).

---

## Design & principles

### Layered architecture

mini_cc has four layers. Each imports only the layers below it — never
upward:

```
┌─────────────────────────────────────────────────────────────┐
│ server/   FastAPI · HTTP/SSE · per-tenant API-key auth       │ ← transport
├─────────────────────────────────────────────────────────────┤
│ session/  SessionManager (per-project RLock serialization)   │ ← orchestration
│ projects/ ProjectManager (tenant metadata + layout + cache)  │
├─────────────────────────────────────────────────────────────┤
│ core/     AgentLoop · hooks · recovery · compaction          │ ← agent core
│ teams/  tools/  skills/  mcp/  scheduler/                    │
├─────────────────────────────────────────────────────────────┤
│ sandbox/  Sandbox Protocol + Subprocess/Container impls      │ ← isolation + storage
│ storage/  Storage Protocol + FSStorage                       │
│ auth/     TenantKeyRegistry (transport-agnostic)             │
└─────────────────────────────────────────────────────────────┘
```

Note: **the SDK core (core/, tools/) has zero knowledge of the HTTP
transport**. `AgentLoop` doesn't know whether it's being called directly
by the SDK or routed through SessionManager by FastAPI. That's bidirectional
freedom: embed mini_cc in your own Python backend for in-process calls, or
deploy it standalone.

### The main request path

The diagram below is the full lifecycle of "Web UI sends one message". It
crosses all four layers and returns to the client:

```
client ─POST /send──▶ routes/sessions.py
                       │
                       ▼  Depends(require_scope("sessions:write"))
                     deps.py ── parse Bearer key
                       │       → check expiry → tenant match → scope
                       ▼
                     SessionManager.send(project_id, session_id, input)
                       │
                       ├── _ensure_warm()  cold session? rebuild loop + history
                       │
                       └── with _lock_for(project_id):  same-project serialize
                             │
                             ▼
                           AgentLoop.run(user_input)
                             │  loop: prep context → stream LLM → run tools
                             │
                             ├──▶ tool.handle(ctx, args)
                             │      └──▶ ctx.sandbox.execute(...)  or .read()
                             │
                             └──▶ yield event  (text / tool_use / tool_result / done)
                                  │
                                  ▼  SSE bridge (worker thread + asyncio.Queue)
                                client
```

Key invariants:

- **Routes and warm sessions for the same project share one `Project`
  object.** `ProjectManager.get()` is cached
  (`mini_cc/projects/manager.py:225`), invalidated by the mtime signature
  of `.mcp.json` / `mcp.toml` / `permissions.toml`. Warm path ~1μs; only
  cold assembly connects MCP and builds schedulers.
- **Same-project serialize, cross-project parallel.**
  `SessionManager.send()` acquires a per-project `RLock`
  (`mini_cc/session/manager.py:183`) so the stateful LLM call can't race
  on one messages file.
- **The HTTP layer never blocks a worker.** `SessionManager.try_lock()`
  is a non-blocking probe — a second concurrent send to the same project
  returns **409 `project_busy`** immediately.

### The two "multiple-impl" interfaces

The most worthwhile extension points in mini_cc are two Protocol + multiple
implementations layers:

| Interface | Implementations | Swapped at |
|-----------|-----------------|------------|
| `Sandbox` | `SubprocessSandbox` / `ContainerSandbox` | `ProjectManager._sandbox_factory` closure (`projects/manager.py:280`) |
| `ContainerRuntime` | `DockerRuntime` / `OpenSandboxRuntime` / `FakeRuntime` | `ServerRuntimeContext._runtime` (`server/runtime_context.py:50`) |
| `Storage` | `FSStorage` (default; swappable) | `ProjectManager.storage_factory` |
| `Tool` | `FunctionTool` / `MCPWrapper` / custom | `AgentLoop._build_tools` |

**Why split this way?** Tools only care "can I read/write/execute", not
whether the code runs locally or in a container. `ContainerSandbox` only
cares "forward the command to a container", not whether it's Docker or
OpenSandbox. Adding a new backend (future K8s/gVisor) means implementing
`ContainerRuntime` — nothing above changes. See [03 — Three-tier Sandbox](03-sandbox.md).

### Tenant-scoped directory layout

This is the physical basis for multi-tenant isolation
(`mini_cc/projects/layout.py:1`):

```
<data_dir>/
  tenants/
    <tenant_id>/
      projects/
        <project_id>/
          workspace/      ← sandbox project_root (where the model works)
          .state/         ← FSStorage root: messages/todos/sessions/...
          meta.json       ← ProjectMeta (carries tenant_id)
      .storage/           ← tenant storage root (one subdir per project)
  keys.json               ← tenant API key registry
```

Two tenants using the **same `project_id`** get completely isolated
workspace + storage. Cross-tenant access returns 404 (not 403) so the
existence of another tenant's project is never leaked.

### Project assembly

`ProjectManager._assemble()` (`mini_cc/projects/manager.py:278`) wires all
of a project's dependencies in one shot. A short excerpt from real source:

```python
# mini_cc/projects/manager.py:278
def _assemble(self, project_id, meta):
    ws = workspace_path(self.root, meta.tenant_id, project_id)
    sandbox = self._sandbox_factory(meta.tenant_id, project_id, ws, self.policy)
    storage = self.storage_factory(self._state_root(meta.tenant_id))
    ...
    project = Project(project_id=project_id, workspace=ws, meta=meta,
                      sandbox=sandbox, storage=storage, ...)
    project.teams = TeammateSpawner(
        workspace=ws,
        loop_factory=lambda sid, _p=project: _build_teammate_loop(_p, sid),
        project_id=project_id, storage=storage)
    _connect_configured_mcp_servers(mcp_pool, ...)  # 3-tier .mini_cc/ discovery
    return project
```

`as_ref()` converts `Project` into a lean `ProjectRef` for `AgentLoop` —
decoupling the loop from assembly details. Teammates derive sub-AgentLoops
via the same `_sandbox_factory` closure, so **container-enabled tenants
automatically propagate the container sandbox to teammates**.

---

## Operation & configuration

### Environment variables (commonly used)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ANTHROPIC_BASE_URL` | — | alternate base URL (proxy, Anthropic-compatible service) |
| `MODEL_ID` | `claude-sonnet-4-6` | primary model |
| `LITELLM_API_KEY` | — | OpenAI-compatible endpoint key (routes `openai/*`, `deepseek/*`, etc.) |
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | root for projects, state, key registry |
| `MINI_CC_HOST` / `MINI_CC_PORT` | `127.0.0.1` / `8000` | server bind |
| `MINI_CC_SANDBOX_BACKEND` | `auto` | `auto` / `opensandbox` / `docker`, see [03](03-sandbox.md) |
| `MINI_CC_SANDBOX_DEFAULT` | `subprocess` | single-tenant default sandbox: `subprocess` / `container` |

Full table in `mini_cc/README.md` under "Configuration".
`AnthropicConfig.has_llm_credentials()` returns whether credentials are
ready — `/health` uses it to avoid false ok.

### CLI

```bash
python -m mini_cc.server                          # default = serve
python -m mini_cc.server keygen <tenant>          # → mck_<32hex>
python -m mini_cc.server keys list <tenant>
python -m mini_cc.server keys rotate <key> [--grace-hours N]
python -m mini_cc.server revoke <key>
python -m mini_cc.server sandbox status [--tid T]
python -m mini_cc.server sandbox stop <tid>
python -m mini_cc.server sandbox build-image [--tag T] [--dockerfile P]
```

---

## Verification steps

The commands below assume the backend runs on `127.0.0.1:8002` (the
frontend's default API base per the README). Start the server first:

```bash
# terminal 1
python -m mini_cc.server keygen my_tenant    # prints mck_<hex>, save as $KEY
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
ANTHROPIC_API_KEY=any-fake-key \
python -m mini_cc.server
```

```bash
# terminal 2 — verify the layered path
# 1. liveness probe (no auth)
curl -s http://127.0.0.1:8002/health
# → {"ok": true, ...}

# 2. create a project (URL tid must match key-resolved tenant)
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"project_id":"demo","display_name":"Demo"}'

# 3. start a session
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/demo/sessions \
     -H "Authorization: Bearer $KEY" -d '{"session_id":"s1"}'

# 4. send a turn — SSE stream, watch text/tool_use/tool_result/done
curl -N -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/demo/sessions/s1/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"list the files here"}'

# 5. cross-tenant isolation: my_tenant key hits other_tenant → 404 not 403
curl -s -o /dev/null -w "%{http_code}\n" \
     http://127.0.0.1:8002/tenants/other_tenant/projects/demo \
     -H "Authorization: Bearer $KEY"
# → 404
```

```bash
# verify the assembly cache + shared Project identity
python -m pytest tests/test_p3_projects.py -q   # see ProjectManager.get cache tests
```

---

## Common pitfalls / debugging

1. **First send raises LLM 401 when using the SDK directly.** The SDK does
   no implicit credential discovery; explicitly calling
   `set_default_config(AnthropicConfig(api_key=...))` is the safe path.
   This was audit P0-5's top SDK pain point. Diagnose with
   `AnthropicConfig.has_llm_credentials()` — False means creds missing.
2. **Frequent `project_busy` 409s.** Concurrent sends to one project are
   serialized by design; a second concurrent send 409s instead of queuing.
   If your client retries serially, confirm the previous send really
   received `done` or the client disconnected — otherwise the server-side
   RLock isn't released yet.
3. **Editing `.mini_cc/.mcp.json` has no effect.** The assembly cache
   invalidates by mtime (`_config_signature`), but `create()` deliberately
   doesn't prime the cache. Force a rebuild with
   `pm.invalidate(pid, tenant_id=tid)`.
4. **On Windows `mkdir -p` creates a directory literally named `-p`.**
   `SubprocessSandbox` prefers bash; only falls back to the platform
   default shell if bash is absent. Install Git Bash or WSL.
5. **Treating the sandbox as a hard security boundary.** `Policy` is a
   defense-in-depth layer; it won't stop a determined adversary. For
   untrusted code, enable the container sandbox (see [03](03-sandbox.md)).

---

## Further reading

- Sibling chapters: [02 — Storage, Projects, Sessions](02-storage-projects-sessions.md) ·
  [03 — Three-tier Sandbox](03-sandbox.md)
- Conceptual: [s01 — The Agent Loop](../../en/s01-the-agent-loop.md) ·
  [s02 — Tool Use](../../en/s02-tool-use.md)
- Source: `mini_cc/README.md` (subsystem map), `mini_cc/ARCH.zh.md` (full Mermaid)
- Advanced: `docs/mini_cc/container-sandbox.md` (P5 container sandbox),
  `docs/zh/2026-06-28-functional-features.zh.md` (F1-F7 feature manual)
