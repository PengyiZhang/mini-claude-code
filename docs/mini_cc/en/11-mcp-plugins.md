[ < [10](10-workflow-v2.md) ] [ [12](12-skills-commands-lsp.md) > ] · [中文版本](../zh/11-mcp-plugins.md)

# 11 — MCP Clients & 3-tier Plugins

> MCP (Model Context Protocol) is Anthropic's "tool bus" standard. mini_cc
> is both an **MCP client** (consuming tools exposed by external servers)
> and a **plugin host** (system / tenant / project `.mini_cc/` directories).
> This chapter unpacks three things: `MCPPool` + the three transports
> (stdio / http / sse), the 3-tier directory layout with merge discovery,
> and failure visibility (`AttemptRecord` so `/mcp` can tell you "why your
> .mcp.json didn't take effect").

---

## Problem & motivation

s20's MCP integration had exactly one path: an in-process `MCPClient` plus
hardcoded servers at startup. In production four problems hit immediately:

1. **Real MCP servers live in subprocesses or remote services**. You need
   to speak three wire formats — stdio transport (local command), HTTP
   Streamable (remote service), SSE (legacy spec) — or you can't talk to
   80% of the servers on the public registry.
2. **Configuration is scattered across N locations**. The sysadmin wants
   `pyright` for every tenant; the tenant admin wants the GitHub MCP for
   their org; the project author wants just one search tool in their own
   workspace. All three tiers must be declarable, with a deterministic
   **project > tenant > system** override order.
3. **Claude Code's `.mcp.json` is the de-facto standard**. Users already
   have this file; mini_cc must consume it verbatim, not force a rewrite
   into a proprietary format. The legacy `mcp.toml` must keep working too.
4. **Failures must be visible**. A server that won't connect shouldn't
   silently vanish from `/mcp` — otherwise the user stares at "no MCP
   servers registered" with no clue what's wrong with their `.mcp.json`.

Source for this chapter: `mini_cc/mcp/{client,stdio,http}.py`,
`mini_cc/plugins/{discover,paths,__init__}.py`, `mini_cc/config.py`, and
the merger at project assembly time
`mini_cc/projects/manager.py:_connect_configured_mcp_servers`.

---

## Design & principles

### 1. MCPPool: factories on the class, connection state on the instance

```
                ┌─ class-level: _factories ──────────┐
                │  {"docs": lambda: MCPClient(...),   │   ← registered at app start
                │   "github": lambda: ...}            │
                └─────────────────────────────────────┘
                              │ available_servers()
                              ▼
MCPPool(project_id="p_demo")
   _clients: {"docs": MCPClient}        ← per-project connection state
   _attempts: {"fs": AttemptRecord(ok=False, msg="...")}   ← failure visibility
```

Factories live on the class, so every project shares the same "connectable"
list; each `MCPPool` instance holds only the clients it actually connected
(`mcp/client.py:73`). Three connect entry points map to three transports:

| Method              | Transport | File                |
|---------------------|-----------|---------------------|
| `connect(name)`     | in-process | `mcp/client.py:117` |
| `connect_stdio()`   | subprocess | `mcp/client.py:133` |
| `connect_http()`    | HTTP       | `mcp/client.py:165` |
| `connect_sse()`     | SSE        | `mcp/client.py:193` |
| `connect_from_spec()` | dispatch on `spec["type"]` | `mcp/client.py:217` |

`connect_from_spec` is the bridge between the discovery layer and the pool
— it returns `(False, message)` instead of raising, so one bad entry
can't abort the auto-connect loop (`mcp/client.py:222`).

### 2. The three wire formats

**StdioMCPClient** (`mcp/stdio.py:38`) spawns a child process and speaks
the LSP-style `Content-Length: <n>\r\n\r\n<json>` framing. A background
reader thread demuxes responses onto waiters by id:

```python
def _write_message(self, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    self._proc.stdin.write(header + body); self._proc.stdin.flush()
```

(`mcp/stdio.py:157`). `startup()` runs the standard `initialize` →
`notifications/initialized` → `tools/list` handshake (`mcp/stdio.py:104`),
normalizing discovered tools into `{name, description, inputSchema}`.

**HttpMCPClient** (`mcp/http.py:142`) speaks MCP's Streamable HTTP: POST
JSON-RPC to a single endpoint, accepting three response shapes —
`application/json` (bare JSON-RPC), `text/event-stream` (SSE frames, take
the first data block matching the id), `application/json-seq` (one JSON
per line). `_parse_remote_response` (`mcp/http.py:344`) handles all three.

**SseMCPClient** (`mcp/http.py:170`) is the legacy spec: GET `url` opens
the event stream, the server emits an `endpoint` event telling us where to
POST; subsequent request/response flows over the same stream.
`_resolve_endpoint` (`mcp/http.py:212`) supports relative paths (resolved
against the SSE URL's scheme+host).

### 3. The three-tier directory layout

Every tier has the same shape (`plugins/paths.py:3`):

```
<tier_root>/.mini_cc/
    skills/<name>/SKILL.md
    memory/<name>.md
    mcp.toml              ← legacy mini_cc format
    .mcp.json             ← Claude Code format
    permissions.toml
```

| Tier | Path | Owner |
|------|------|-------|
| **system** | `<data_dir>/.mini_cc/` | operator |
| **tenant** | `<data_dir>/tenants/<tid>/.mini_cc/` | tenant admin |
| **project** | `<workspace>/.mini_cc/` | project author |

`project_tier_dirs(data_dir, tenant_id, workspace)` (`plugins/paths.py:71`)
returns `[system, tenant, project]` in that order — later wins.

`tier_dir` validates `tenant_id` against `^[A-Za-z0-9_-]+$`
(`plugins/paths.py:35`); otherwise a malicious tid could `../` out of the
`tenants/` tree.

### 4. Two file formats for MCP servers

`discover_mcp_servers(tier_dirs)` (`plugins/discover.py:340`) walks the
three tiers; within each tier it reads `mcp.toml` (legacy) first and then
overlays `.mcp.json` (`_read_tier` at `plugins/discover.py:332`). The
`.mcp.json` is Claude Code's format:

```json
{"mcpServers": {
  "github": {"type":"stdio","command":"npx",
             "args":["-y","@modelcontextprotocol/server-github"],
             "env":{"GITHUB_TOKEN":"ghp_..."}}
}}
```

`mcp.toml` is mini_cc's native format, where `command` is already a list:

```toml
[docs]
command = ["npx", "mcp-server-docs"]
[docs.env]
KEY = "value"
```

`_normalize_spec` (`plugins/discover.py:214`) coerces both shapes into the
unified `{type, command, env, cwd, url, headers}`. **Type inference** is
key: Claude Code lets you omit `type` — having `url` implies http, having
`command`/`args` implies stdio (`plugins/discover.py:237`).

### 5. The merger: env override applies last, wins last

`_connect_configured_mcp_servers` (`projects/manager.py:349`) is the merge
entry point at project assembly:

```python
# 1) Merge three .mini_cc/ tiers (system → tenant → project)
tier_dirs = project_tier_dirs(data_dir, tenant_id, workspace)
servers = discover_mcp_servers(tier_dirs)
# 2) env / programmatic override last → wins
env_servers = default_config().mcp_servers or {}    # from MINI_CC_MCP_SERVERS
for k, v in env_servers.items():
    if "type" not in v: v = {**v, "type": "stdio"}
    servers[k] = v
# 3) Dispatch each spec
for name, spec in servers.items():
    pool.connect_from_spec(name, spec)
```

`MINI_CC_MCP_SERVERS` is JSON (`config.py:48`) with the same shape as
`mcp.toml`:

```json
{"docs": {"command": ["npx","mcp-server-docs"], "env": {...}}}
```

Why does env apply last? It's the escape hatch for ops to override a tier
config without touching files or restarting — set the env var and the next
project assembly picks it up.

### 6. AttemptRecord: failure visibility

Every connect path — success or failure — records an `AttemptRecord(name,
ok, message)` (`mcp/client.py:30`):

```python
@dataclass
class AttemptRecord:
    name: str; ok: bool; message: str
```

The `/mcp` command reads `pool.list_attempts()` (`commands/registry.py:370`)
and lists discovered-but-failed servers in their own section:

```
**failed to connect (discovered in .mcp.json/mcp.toml):**
- 🔴 `github` — MCP server `github`: command not found: 'npx'
  _edit the matching entry in `.mini_cc/.mcp.json` ..._
```

Without this, a failed server would vanish from both the `connected` and
`available` lists, leaving the user staring at "no MCP servers registered"
with no diagnostic.

### 7. Tool namespacing: `mcp__<server>__<tool>`

`MCPPool.all_tools()` (`mcp/client.py:280`) wraps each MCP tool as a
`FunctionTool` named `mcp__{safe_server}__{safe_tool}`, where non-alphanumeric
characters are replaced with `_` (`normalize_mcp_name`, `mcp/client.py:26`).
Builtin tools and MCP tools then merge transparently in the AgentLoop's
tool pool — the model sees one unified list.

---

## Operation & configuration

### Per-transport spec fields (after normalization)

| Field | stdio | http/sse |
|-------|-------|----------|
| `type` | `"stdio"` | `"http"` / `"sse"` |
| `command` | `["npx","-y","pkg"]` | — |
| `args` | (Claude Code style; merged into command) | — |
| `env` | `{...}` | `{...}` (optional) |
| `cwd` | `"..."` | — |
| `url` | — | `"https://..."` |
| `headers` | — | `{"Authorization":"Bearer ..."}` |

### Environment variables

| Variable | Purpose | Shape |
|----------|---------|-------|
| `MINI_CC_MCP_SERVERS` | Programmatic MCP server registration (JSON) | `{"name":{"command":[...],"env":{...}}}` |

Source: `mini_cc/config.py:48`, `:121`.

### Built-in server template

`mini_cc/sandbox/templates/opensandbox.mcp.json` is a ready-made template
for the OpenSandbox MCP server; `${OPEN_SANDBOX_API_KEY}` is expanded from
env at spawn time.

### Debug slash commands

| Command | Purpose | Source |
|---------|---------|--------|
| `/mcp` | List connected / available / failed-to-connect | `commands/registry.py:370` |
| `/tools` | List every builtin + MCP tool | `commands/registry.py:205` |

---

## Verification steps

```bash
# 1) Drop a minimal .mcp.json into the project workspace
mkdir -p /tmp/ws_demo/.mini_cc
cat > /tmp/ws_demo/.mini_cc/.mcp.json <<'JSON'
{"mcpServers": {
  "time": {"type":"stdio","command":"python","args":["-c",
    "import sys,json\nprint(json.dumps({'jsonrpc':'2.0','id':1,'result':{'tools':[{'name':'now','description':'current time','inputSchema':{'type':'object'}}]}}))"]},
  "broken": {"type":"stdio","command":"this-binary-does-not-exist","args":[]}
}}
JSON

# 2) Start the backend (assume data_dir under /tmp, workspace=/tmp/ws_demo),
# warm a session, and in chat send:
/mcp

# Expected output:
#   connected:
#   - 🟢 time (1 tools live)
#   failed to connect (discovered in .mcp.json/mcp.toml):
#   - 🔴 broken — MCP server `broken`: command not found: 'this-binary-does-not-exist'

# 3) Override one more via env
export MINI_CC_MCP_SERVERS='{"fromenv":{"command":["python","-m","mcp_server_time"]}}'
# Restart the backend, send /mcp in chat — `fromenv` should appear under connected
```

Driving a transport directly from Python:

```python
from mini_cc.mcp.client import MCPPool
pool = MCPPool("p_demo")
ok, msg = pool.connect_stdio("time", ["python","-m","mcp_server_time"])
print(ok, msg)             # True, "Connected ... Discovered N tools: now"
print(pool.all_tools())    # [FunctionTool(name="mcp__time__now", ...)]
```

---

## Common pitfalls / debugging

1. **Edited `.mcp.json` but `/mcp` didn't change?** Discovery runs once at
   **project assembly** (`projects/manager.py:327`). After editing the
   file, reopen the session / restart the process. Skills have
   `rescan_skills` but MCP does not.
2. **stdio server keeps `command not found`?** `command` is normalized to a
   list (`plugins/discover.py:252`); the first element must be an
   executable on PATH. Write `"command":"npx","args":["-y","pkg"]` — a
   literal `"command":"npx -y pkg"` would treat the whole string as the
   executable name.
3. **HTTP server reports "non-object" or "non-JSON"?**
   `_parse_remote_response` accepts only `application/json`,
   `text/event-stream`, `application/json-seq` (`mcp/http.py:344`). An HTML
   error page from a misconfigured proxy makes it blow up — `curl -i` the
   endpoint and check Content-Type first.
4. **SSE client `endpoint event timeout`?** `SseMCPClient` GETs the url
   and waits for the `endpoint` event; some legacy servers don't send one,
   or a proxy strips it (`mcp/http.py:205`). These servers usually work
   better with `HttpMCPClient`.
5. **Spec silently dropped because `type` was missing?** When neither
   `command` nor `url` is present, `_normalize_spec` returns `None`
   (`plugins/discover.py:243`) and the server is silently skipped. Confirm
   your spec has at least one.
6. **tenant_id with `..` or `/` rejected?** `tier_dir` enforces
   `^[A-Za-z0-9_-]+$` (`plugins/paths.py:35`) — it's a path-traversal
   guard, not a bug.

---

## Further reading

- Source: `mini_cc/mcp/{client,stdio,http}.py`, `mini_cc/plugins/{discover,paths}.py`
- Merger: `mini_cc/projects/manager.py:349` (`_connect_configured_mcp_servers`)
- Env config: `mini_cc/config.py:48` (`_mcp_servers_from_env`)
- Template: `mini_cc/sandbox/templates/opensandbox.mcp.json`
- Previous chapter: [10 — Workflow V2](10-workflow-v2.md)
- Next chapter: [12 — Skills, Commands, LSP](12-skills-commands-lsp.md)
