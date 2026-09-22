English | [中文](./README.zh.md)

# mini_cc — a multi-tenant, backend-integrable mini Claude Code

A multi-tenant, sandboxed, backend-integrable reimplementation of the Claude
Code harness: an agent framework you can embed as a Python SDK, run as a
FastAPI HTTP/SSE service, and drive from the bundled web console.

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

## Highlights

- **Multi-tenant by construction** — every subsystem is per-project with no
  module-level global state; per-tenant API keys with scopes, expiry, and
  rotation; cross-tenant access leaks nothing (404, not 403).
- **Agent core** — streaming `AgentLoop`, layered context compaction with
  transcript snapshots, retry / token-escalation / model-fallback recovery,
  hooks, interactive permissions, subagents, on-demand skill loading.
- **Team orchestration** — JSONL message-bus mailboxes, plan-approval
  protocols, teammate spawner with idle-poll task claiming, per-task git
  worktrees, lead watcher with autonomous wakeups.
- **IM channels** — a bidirectional channel abstraction binding sessions to
  external messengers; Feishu (Lark) webhook in/out included.
- **Two-layer sandbox** — host `SubprocessSandbox` (path validation + command
  policy + filtered env) with an optional per-tenant Docker container layer
  that auto-degrades when Docker is unavailable.
- **Observability** — Prometheus `/metrics`, per-tenant token attribution,
  opt-in OpenTelemetry tracing, `X-Trace-Id` correlation end to end.
- **Web console** — Vite + React 19 + TypeScript dark UI: SSE chat with
  foldable activity cards, inline permission prompts, file tree with
  upload/preview/ZIP download, session cold/warm management, admin key &
  metrics dashboards, team timeline, channels panel. Playwright e2e suite.
- **Well tested** — 150+ pytest files covering auth, isolation, SSE, sandbox,
  teams, channels, cron, MCP, plugins, and workflow.

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
│  web/      React console (chat, files, sessions, admin, team)   │
├─────────────────────────────────────────────────────────────────┤
│  server/   FastAPI, HTTP/SSE, API-key auth, scopes, metrics     │  ← transport
├─────────────────────────────────────────────────────────────────┤
│  session/  SessionManager (project-scoped locking, resume)      │  ← orchestration
│  projects/ ProjectManager (tenant metadata, layout)             │
├─────────────────────────────────────────────────────────────────┤
│  core/     AgentLoop, hooks, recovery, compaction, subagent     │  ← agent core
│  teams/    MessageBus, ProtocolTracker, TeammateSpawner         │
│  channels/ IM channel bindings (Feishu)                         │
│  tools/    bash, fs, todo, cron, task, worktree, mcp, ...       │
│  skills/   on-demand skill loading        mcp/  MCP client/pool │
│  scheduler/ CronScheduler               workflow/ approvals    │
├─────────────────────────────────────────────────────────────────┤
│  sandbox/  SubprocessSandbox + Docker container layer + Policy  │  ← isolation
│  storage/  pluggable Storage interface, FSStorage default       │
│  auth/     TenantKeyRegistry (transport-agnostic)               │
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
| [`docs/mini_cc/`](./docs/mini_cc/)    | 14-chapter bilingual internals deep-dive      |
| [`docs/plans/`](./docs/plans/)        | design docs for each phase (auth, sandbox, teams, channels, ...) |
| [`reference/learn-claude-code/`](./reference/learn-claude-code/README.md) | upstream teaching tree, kept for study |

## Documentation

- [`mini_cc/README.md`](./mini_cc/README.md) — full framework reference:
  API surface, HTTP endpoints, security model, configuration, sandbox,
  testing, web UI.
- [`docs/mini_cc/`](./docs/mini_cc/README.md) — *Advanced Internals*: a
  14-chapter module-by-module deep dive (bilingual zh/en), each chapter with
  file:line citations and runnable verification steps.
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
