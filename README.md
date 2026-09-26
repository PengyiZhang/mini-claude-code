English | [中文](./README.zh.md)

[![CI](https://github.com/PengyiZhang/mini-claude-code/actions/workflows/ci.yml/badge.svg)](https://github.com/PengyiZhang/mini-claude-code/actions/workflows/ci.yml)

# mini_cc — a multi-tenant, backend-integrable mini Claude Code

A multi-tenant, sandboxed, backend-integrable reimplementation of the Claude
Code harness: an agent framework you can embed as a Python SDK, run as a
FastAPI HTTP/SSE service, and drive from the bundled web console — with
multi-agent teammates, gated workflows, and bidirectional IM channels built
in.

![Demo](assets/mini-cc-demo-video.gif)

```
Agency comes from the model. mini_cc gives the model hands, eyes,
a workspace, teammates, and a network surface.
```

![mini_cc web console](docs/mini_cc/zh/img/01-fresh-session.png)

The model is the driver; this project is the vehicle. mini_cc started as a
production-oriented port of the 19-subsystem harness pattern taught by
[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) (whose
teaching tree is preserved under [`reference/`](./reference/learn-claude-code/)
for study), and grew into a full platform: per-project isolation, pluggable
storage, no module globals, and an optional HTTP/SSE transport.

## Features

### Multi-tenant platform

- Every subsystem is per-project with **no module-level global state**
- Per-tenant API keys with **scopes, expiry, and rotation** (grace-period
  old-key acceptance); cross-tenant access returns 404 — resource existence
  never leaks
- Per-tenant token-bucket rate limiting, Prometheus `/metrics` with per-tenant
  token attribution, opt-in OpenTelemetry tracing, `X-Trace-Id` correlation
- Project creation from **templates** (blank, Python CLI, skill starter)
- Signed read-only **share links** (`sh_...` HMAC tokens with TTL) exposing an
  embeddable transcript page — plus outbound **webhooks** with HMAC-signed
  payloads and SSRF-protected targets

### Agent core

- Streaming `AgentLoop`; layered context **compaction** with transcript
  snapshots; retry / token-escalation / model-fallback **recovery**
- **Hooks** and opt-in **interactive permissions** (`permissions.toml` gates
  tools behind SSE permission prompts with decide/timeout flow)
- Subagents, on-demand **skill loading** (SKILL.md packs), three-tier
  declarative **memory** (system → tenant → project, keyword recall,
  tool-gated writes)
- **MCP** client pool per project (`/mcp connect|tools|reconnect`)
- **LSP tool**: on-demand language servers (pyright, clangd, TS, rust-analyzer,
  gopls, ...) exposing go-to-definition / references / call hierarchy
- Per-project **cron** scheduler plus second-precision one-shot **wakeups**
  for agent self-pacing; background task offloading with live status

### Multi-agent teammates

- Spawn named teammates (`/agents spawn <name> <role> --prompt ...`), each a
  full sub-AgentLoop in its own thread — stop / edit / delete from the roster
- **JSONL message bus**: per-teammate mailboxes with append-only history,
  broadcast with shared ids, auto-CC of result/milestone/blocker to the lead
- **Plan-approval gate**: `submit_plan` → `review_plan` handshake blocks a
  teammate's next turn
- **Autonomous execution**: idle-polling auto-claim of unclaimed tasks
  (respecting `blockedBy` deps, auto-cd into the task's git worktree);
  park-after-result; scheduled self-wakeups
- **`@mention` routing** (CJK names included) straight to a teammate's inbox;
  inbox read/unread/ignored disposition with ack/ignore
- **LeadWatcher** daemon nudges the lead when teammate results arrive while
  you're away

### Workflows

- **Dynamic workflows (V1)**: prompt-step chains defined as dict or markdown
  sections; per-step conditions, parallel steps, retry/skip/abort policies,
  `{step_id}` result substitution; `/workflow save|load|list` persists them
- **Workflow V2**: versioned definitions with typed steps — `action`,
  `validate`, `checkpoint` (human approval), `webhook_wait`, `email_wait`;
  soft-condition branches and explicit `next` ladders/loops; triggers:
  manual / webhook / schedule / email
- **Approval gates** pause a run and enumerate approver candidates (alive
  teammates → stopped → lead → free-form human); resolve via UI buttons,
  inbound webhook payload, or inbound email
- Three-pane **workflow console** (definitions/runs, event timeline with
  approve/reject, step inspector) + a per-session **run table** of active
  workflows and background tasks

### IM channels

- **Bidirectional channel abstraction**: bind a session to an external
  messenger — inbound chat becomes agent turns, agent/teammate events push
  back out; per-channel event-type filter; secrets masked on read
- **Feishu (Lark) built in**: URL-verification handshake, signature check,
  AES-encrypted envelope decryption, cached `tenant_access_token` outbound;
  new kinds register via `register_channel_kind`

### Sandbox & isolation

- Host `SubprocessSandbox`: path whitelist, command policy, filtered env
- Optional per-tenant **Docker container layer** (bind-mounted workspaces,
  declarative apt/pip/node packages, CPU/memory limits, extra mounts) that
  **auto-degrades** to subprocess when Docker is unavailable

### Web console & commands

- Vite + React 19 + TypeScript dark UI: SSE chat with foldable activity
  cards and inline permission prompts, file tree (upload / folder upload /
  preview / ZIP download), session cold/warm management, team timeline and
  teammates panel, todo panel, channels panel, admin key & metrics dashboards;
  **Mermaid diagram rendering** in chat; Playwright e2e suite
- **20+ server-side slash commands** (`/agents`, `/workflow`, `/channels`,
  `/sessions`, `/cost`, `/model`, `/permissions`, `/loop`, `/bg`, `/skills`,
  `/mcp`, `/tasks`, `/config`, `/search`, `/fork`, `/export`, ...) emitting
  typed **card events** (list / table / key-value / steps) with action
  buttons and live auto-refresh

### Tested

150+ pytest files covering auth, tenant isolation, SSE, sandbox, teams,
channels, cron, MCP, plugins, workflows, and share tokens.

## Quick start

```bash
pip install -r requirements.txt

# provision a tenant key
python -m mini_cc.server keygen my_tenant        # → mck_<32hex>

# start the server (defaults to 127.0.0.1:8000)
python -m mini_cc.server
```

Talk to it from any language over HTTP/SSE:

```bash
curl -X POST http://localhost:8000/tenants/my_tenant/projects \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"project_id":"demo"}'

curl -N -X POST \
     http://localhost:8000/tenants/my_tenant/projects/demo/sessions/s1/send \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"user_input":"hello"}'
```

Or embed it in-process as an SDK:

```python
from mini_cc import ProjectManager, SessionManager

pm = ProjectManager("./mini_cc_data")
pm.create(tenant_id="t1", project_id="demo")
sm = SessionManager(pm)
sess = sm.start_session("demo")
for ev in sm.send("demo", sess.session_id, "list the files here"):
    print(ev["type"])
```

### Web console

```bash
cd mini_cc/web
npm install && npm run dev        # http://localhost:5173
```

Sign in with the `mck_<hex>` key from `keygen`. Set `ANTHROPIC_BASE_URL` /
`ANTHROPIC_API_KEY` / `MODEL_ID` to point at Anthropic or any
Anthropic-compatible proxy (e.g. LiteLLM).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  web/       React console (chat, files, run, channels, team,    │
│             workflow, admin, todos)                             │
├─────────────────────────────────────────────────────────────────┤
│  server/    FastAPI, HTTP/SSE, API-key auth, scopes, metrics    │  ← transport
│  channels/  bidirectional IM bindings (Feishu)                  │
│  sharing/   signed share links, outbound webhooks               │
├─────────────────────────────────────────────────────────────────┤
│  session/   SessionManager (project-scoped locking, resume)     │  ← orchestration
│  projects/  ProjectManager (tenant metadata, templates)         │
├─────────────────────────────────────────────────────────────────┤
│  core/      AgentLoop, hooks, recovery, compaction, subagent    │  ← agent core
│  teams/     MessageBus, ProtocolTracker, TeammateSpawner,       │
│             LeadWatcher, @mention routing                       │
│  workflow/  V1 dynamic runner + V2 gated runs & approvals       │
│  tools/     bash, fs, todo, cron, task, worktree, mcp, lsp, ... │
│  skills/    on-demand skill loading       memory/ 3-tier memory │
│  mcp/       MCP client/pool              plugins/ 3-tier plugins│
│  scheduler/ CronScheduler + wakeups      commands/ cards & CLI  │
├─────────────────────────────────────────────────────────────────┤
│  sandbox/   SubprocessSandbox + Docker container layer + Policy │  ← isolation
│  storage/   pluggable Storage interface, FSStorage default      │
│  auth/      TenantKeyRegistry (transport-agnostic)              │
└─────────────────────────────────────────────────────────────────┘
```

Each layer imports only the layers below it. The SDK core has no knowledge
of the HTTP transport. See [`mini_cc/ARCH.zh.md`](./mini_cc/ARCH.zh.md) for
a Mermaid-diagram tour of every module and connection.

## Project layout

| Path                                  | What it is                                    |
| ------------------------------------- | --------------------------------------------- |
| [`mini_cc/`](./mini_cc/README.md)     | the framework: SDK, server, tools, web console |
| [`tests/`](./tests/)                  | pytest suite (150+ files)                     |
| [`docs/mini_cc/`](./docs/mini_cc/)    | 15-chapter bilingual internals deep-dive      |
| [`docs/plans/`](./docs/plans/)        | design docs for each phase (auth, sandbox, teams, channels, ...) |
| [`reference/learn-claude-code/`](./reference/learn-claude-code/README.md) | upstream teaching tree, kept for study |

## Documentation

- [`mini_cc/README.md`](./mini_cc/README.md) — full framework reference:
  API surface, HTTP endpoints, security model, configuration, sandbox,
  testing, web UI.
- [`docs/mini_cc/`](./docs/mini_cc/README.md) — *Advanced Internals*: a
  module-by-module bilingual deep dive, each chapter with file:line
  citations and runnable verification steps.
- [`mini_cc/DEPLOYMENT.md`](./mini_cc/DEPLOYMENT.md) — deployment notes.

## Acknowledgments

This project derives from
[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
— an excellent harness-engineering course whose 20 progressive sessions
teach how to build a Claude Code-style agent from scratch. The upstream
teaching tree is preserved in full under
[`reference/learn-claude-code/`](./reference/learn-claude-code/README.md)
so readers can trace where mini_cc's patterns came from; mini_cc itself is
a ground-up multi-tenant restructuring of those ideas for production use.

Heartfelt thanks to the upstream authors and contributors.

## License

[MIT](./LICENSE)
