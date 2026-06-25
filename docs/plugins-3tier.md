# Three-Tier Plugin Discovery

Mini_cc loads **skills** and **MCP servers** from three layered
directories, mirroring how operators actually manage deployments:

```
system    <data_dir>/.mini_cc/                         ← operator-wide
tenant    <data_dir>/tenants/<tid>/.mini_cc/            ← per customer
project   <workspace>/.mini_cc/                         ← per codebase
```

Later tiers win on name clash. Project > tenant > system — closest to
the code wins, exactly like `.git/config` overrides `~/.gitconfig`.

Each tier has the same shape:

```
.mini_cc/
  skills/<skill-name>/SKILL.md      # YAML frontmatter + markdown body
  .mcp.json                         # Claude Code format (recommended)
  mcp.toml                          # legacy mini_cc format (still works)
  permissions.toml                  # policy overrides
```

## Skills

Drop a `SKILL.md` anywhere in the chain:

```markdown
---
name: my-helper
description: One-line hint the model sees when deciding whether to load it.
---

# My Helper skill body

Detailed instructions the agent receives when it calls `load_skill my-helper`.
```

A skill becomes visible to the agent via the standard `load_skill` tool.
The catalog is refreshed on every `/skills` invocation, so hot-reload
"just works".

## MCP servers — Claude Code format (`.mcp.json`)

This is the recommended shape because it is byte-for-byte compatible
with Claude Code's `.mcp.json`. **Copy a working Claude Code config
into `.mini_cc/` and it works unchanged.**

```json
{
  "mcpServers": {
    "docs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-docs"]
    },
    "remote": {
      "type": "http",
      "url": "https://mcp.example.com/sse",
      "headers": { "Authorization": "Bearer $TOKEN" }
    },
    "old": {
      "type": "sse",
      "url": "https://legacy.example.com/sse"
    }
  }
}
```

### Type inference

| Has                     | Inferred type |
| ----------------------- | ------------- |
| `url`                   | `http`        |
| `command` or `args`     | `stdio`       |
| Explicit `type` field   | that type     |

The three valid types are `stdio`, `http`, `sse`. Anything else is
silently dropped (with a log warning).

### Stdio shape

| Field      | Type             | Notes                                     |
| ---------- | ---------------- | ----------------------------------------- |
| `command`  | string           | Executable name (e.g. `"npx"`)            |
| `args`     | list of strings  | Argv tail (e.g. `["-y", "pkg"]`)          |
| `env`      | object           | Extra environment variables               |

### HTTP / SSE shape

| Field     | Type   | Notes                                                |
| --------- | ------ | ---------------------------------------------------- |
| `url`     | string | Endpoint URL                                         |
| `headers` | object | Request headers (e.g. auth bearer)                   |

`http` uses MCP's Streamable HTTP transport (single endpoint, JSON-RPC
over POST, response can be plain JSON or `text/event-stream`). `sse`
uses the older transport (GET to learn the POST endpoint, then
responses stream back over the same SSE channel). Modern servers should
prefer `http`.

## MCP servers — legacy TOML format (`mcp.toml`)

The original mini_cc shape. Still works. Each entry is a TOML table
with a `command` *list* and an optional `env` sub-table:

```toml
[docs]
command = ["npx", "mcp-server-docs"]

[docs.env]
API_KEY = "value"
cwd     = "/path"   # optional
```

### Mixing the two

Within one tier, **`.mcp.json` overrides `mcp.toml` on name clash**.
Distinct keys from both files all appear in the merged catalog. This
keeps existing deployments working while new ones migrate to the JSON
format.

Across tiers, later tiers override earlier ones regardless of which
file format they used.

## Auto-bootstrap

`ProjectManager.create(tenant_id, project_id)` materialises:

- `<data_dir>/tenants/<tid>/.mini_cc/skills/`   (first project under a tenant)
- `<workspace>/.mini_cc/skills/`                 (every new project)

Server CLI boot ensures `<data_dir>/.mini_cc/` exists on boot. You can
drop skills + `.mcp.json` into any of these at any time; the next
`/skills` or `/mcp` invocation picks them up (skills) or the next
project assembly connects them (MCP).

## Environment escape hatch

`MINI_CC_MCP_SERVERS` still works. It applies last and overrides
everything from disk — useful for CI and emergency overrides:

```bash
export MINI_CC_MCP_SERVERS='{"ci-docs": {"command": ["./fake-docs.sh"]}}'
```

(JSON shape, same per-entry layout as `.mcp.json` entries.)

## Programmatic API

```python
from mini_cc.plugins import (
    PluginTier, tier_dir, ensure_tier_dir,
    project_tier_dirs,
    discover_skills, discover_mcp_servers,
)

sys_dir = tier_dir(data_dir, PluginTier.SYSTEM)
ten_dir = tier_dir(data_dir, PluginTier.TENANT, tenant_id="acme")
proj_dir = tier_dir(workspace, PluginTier.PROJECT)

# Project assembly uses this helper:
tier_dirs = project_tier_dirs(data_dir, "acme", workspace)

skills = discover_skills(tier_dirs)
servers = discover_mcp_servers(tier_dirs)   # unified shape, type-dispatched
```

`discover_mcp_servers` always returns the unified shape
`{name: {"type": "stdio"|"http"|"sse", ...}}` regardless of which file
format it came from. Pass each entry to `MCPPool.connect_from_spec()`.

## Inspecting what loaded

```
/skills       # list skills visible to this project
/mcp          # list connected + available MCP servers
/tools        # list every tool (builtin + MCP) the agent can call
```

The `/tools` command is the answer to "is my web_search / mcp__foo__bar
actually visible to the agent?" — it shows the same list the Anthropic
API sees in the `tools` field.
