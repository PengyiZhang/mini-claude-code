[ < [14](14-web-ui-observability-deploy.md) ] · [中文版本](../zh/15-evolution-migration.md)

# 15 — From teaching framework to production-grade

> The previous fourteen chapters dissected mini_cc *as it is today*. This chapter is about how
> it got that way — an evolution reconstructed along the git timeline. mini_cc was never written
> in one sitting. It grew out of `c6a27ef`, an 11-session teaching repo, through P0-P6
> infrastructure, Phase A-F server hardening, Phase G-J capability expansion, the round-2
> production audit, and Workflow V2 (W1-W6 + B8). Each step has a "why it was needed" and "what
> it broke." This chapter threads the evolution logic scattered across 60+ commits into a single
> story line — which decisions were path-dependent, which were design invariants, and why
> "tutorial code that runs" and "production code that ships" are separated by ten gates.

---

## Starting point: 11 progressive sessions

The repo's origin is `c6a27ef feat: build an AI agent from 0 to 1 -- 11 progressive sessions`.
This commit established the `agents/` directory — twelve single-file Python teaching samples:
`s01_agent_loop.py` (120 lines) on the minimal agent loop, `s02_tool_use.py` on tool dispatch,
all the way to `s11_autonomous_agents.py` (586 lines) on autonomous teams,
`s12_worktree_task_isolation.py` on git worktree isolation, plus an `s_full.py` aggregate. Each
session adds one mechanism — single file, single tenant, in-memory state. This shape is ideal
for learning: open `s01`, read 120 lines end to end, no abstraction layers, no Protocols, no
directory layout. But those very absences are why it **cannot survive production**:

1. **Global state.** `s11`'s storage, scheduler, and mcp_pool are module-level singletons. Run
   two users in the same process and their messages, tasks, and cron bleed into each other —
   whoever `cd`s first mutates the global cwd.
2. **Synchronous blocking function.** `agent_loop()` is a sync function returning a list. To
   use it from a backend you must wrap transport, concurrency, and auth yourself — and every
   team reinvents this layer.
3. **No tenant boundary.** All state lands under cwd; cross-user isolation relies on the
   convention of "different directories," not an enforced constraint.

The real value of these sessions was walking through the 19 Claude Code subsystems one by one,
producing a checklist of "what capabilities we must keep." mini_cc's goal in one sentence:
**keep every capability of `s_full`, but refactor it into a multi-tenant, backend-embeddable
framework with no module-level global state**.

---

## Step 1: framework scaffolding (P0+P1+P2)

Commit `76647ac feat: scaffold mini_cc multi-tenant agent framework (P0+P1+P2)` landed 2255
lines in one shot, translating the teaching code into a framework skeleton. Three decisions
defined the boundary of every later evolution:

**Decision one: Storage as a Protocol.** `mini_cc/storage/base.py:51` defines
`Storage(Protocol)`, covering messages/todos/tasks/memory/cron/sessions/search/workflow — 19
methods. The default is `FSStorage` (`storage/fs.py:48`) — atomic writes
(`_atomic_write_json` at `fs.py:146`) plus per-key `threading.Lock`. Abstracting storage as a
Protocol is the root of every later "swappable implementation" decision: Workflow V2 adds
table entries without touching the SDK core, P0-3 fixes atomic writes without touching upper
layers, a future Redis/PG backend is just another Storage. The key is **Protocol before
implementation** — the other way binds every call site to a concrete class.

**Decision two: projects / sessions split.** `ProjectManager` at
`mini_cc/projects/manager.py:123` manages tenant metadata + directory layout + assembly cache;
`SessionManager` in `mini_cc/session/manager.py` manages "multi-session orchestration within a
project + per-project RLock serialization." Splitting these two means "assembling a project"
(connect MCP, build scheduler, bind sandbox — expensive, cached) and "running one turn"
(high-frequency, locked) are separate concerns. `ProjectManager._assemble()`
(`projects/manager.py:278`) wires all dependencies in one pass; assembly is invalidated by the
mtime of `.mcp.json`/`permissions.toml` (`get()` caches at `projects/manager.py:225`).

**Decision three: multi-tenant directory layout.** `mini_cc/projects/layout.py` defines the
tenant-scoped layout: `<data_dir>/tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}`.
Two tenants using the same `project_id` get fully isolated workspace + storage; cross-tenant
access always returns 404 (not 403, leaking no information about project existence). This
physical layout is the root of multi-tenant isolation — every later isolation feature (Phase D
scopes, round-2 R2 tests) grows from this root.

P0+P1+P2 is not "implemented part of the functionality" — it is **the framework's invariants
nailed down**: Protocol-based extension points, no module-level global state, tenant isolation
from day 1. The next nine steps all stand on these three invariants.

---

## Step 2: HTTP/SSE transport (P3)

Commit `3b27fb7 feat(server): HTTP/SSE transport layer (P3)` (1406 lines) is the first time
mini_cc crosses a network boundary. `agent_loop()` is synchronous blocking, but HTTP clients
want to stream tokens — a bridge is needed. `mini_cc/server/sse.py` (76 lines) implements it:
a worker thread runs the synchronous `SessionManager.send()`, feeding yielded events through an
`asyncio.Queue` to the ASGI streaming response, framed as `id: <seq>\ndata: <json>\n\n`.
`mini_cc/server/routes/sessions.py` (112 lines) exposes `POST /send` returning
`text/event-stream`.

The transport layer (`mini_cc/server/`) is deliberately blind to the SDK core: `AgentLoop` does
not know whether it is invoked by an HTTP route or directly by the SDK. This decoupling lets
mini_cc be used both as an SDK (`import mini_cc`) and as a service
(`python -m mini_cc.server`). But this step planted two time bombs: SSE is one-way, so the
server does not know when the client disconnects; and a reconnect would re-dispatch the LLM
from scratch. Both were not fully fixed until B8 (`aaa5793`). HTTP routing is in
[07](07-http-server.md); the SSE bridge and framing are in [08](08-sse-streaming.md).

---

## Step 3: React web UI (P4)

Commit `475ac17 feat(web): React UI for mini_cc (P4) + resource mgmt + Playwright e2e` (5509
lines) is mini_cc's first browser interface. The stack: Vite + React + react-router-dom +
zustand + Tailwind. This step did three things beyond "build a page":

1. **HashRouter over BrowserRouter.** In production the frontend is mounted same-origin by
   FastAPI, so `#/projects/...` anchor routing works for deep links without server config. This
   decision in P4 saved rework when Phase F shipped same-origin deployment.
2. **Hand-written SSE client with fetch + ReadableStream.** `EventSource` does not support POST
   bodies or `Authorization` headers. `mini_cc/web/src/lib/sse.ts` uses `fetch()` POST +
   `getReader()` to parse SSE frames manually. Reconnection logic was not added until B8, but
   the skeleton was set in P4.
3. **Resource management.** The Workspace page came with FileTree, FilePreview, RunTable — the
   first time "the agent is working" became visible. Playwright e2e was introduced in the same
   step, becoming the safety net for every later round.

The full Web UI architecture (routing, auth gates, admin surfaces, observability, same-origin
deployment) is in [14](14-web-ui-observability-deploy.md). The significance of P4 is not "now
there's a UI" — it is that **for the first time mini_cc could be used by someone who cannot
curl**. That drew the line between "teaching framework" and "product framework" explicitly.

---

## Step 4: server hardening (Phases A-F)

P0-P4 made mini_cc "runnable," but not production-runnable. The six rounds of hardening in
Phases A-F are six "production gates" — each one is something the teaching version lacks and
production requires:

**Phase A** (`a5fffa2`) — pyproject + mid-turn cancellation + rate limiting + logging.
`agent_loop()` can run for tens of minutes; if the client disconnects, the server keeps burning
tokens. Phase A adds a cancel token to SessionManager that AgentLoop checks every turn (later
`83413f9` added the `cancelled` SSE event, see step 8). Rate limiting blocks runaway clients.

**Phase B** (`68b51ba`) — session resume across restarts. The teaching version holds all
session state in memory; restart and everything is lost. Phase B persists history to
`FSStorage`, and on restart `_ensure_warm()` rebuilds the AgentLoop + history from disk — the
watershed between "can demo" and "can deploy."

**Phase C** (`c154539`) — interactive permission prompts. When bash wants to run `rm -rf`, the
teaching version just runs it; production must pause, ask the user, wait for approval. Phase C
adds a permission gate to tool invocation.

**Phase D** (`6569070`) — scopes / expiry / rotation. The teaching version's API key is a
master key — once leaked, everything is exposed. Phase D adds scopes (`sessions:write`,
`projects:read`, etc.), expiry, rotation with grace periods. Tenant isolation + scopes form the
authz loop, see [09](09-auth.md).

**Phase E** (`6dbb92e`) — metrics + tracing + token attribution. Production must chase slow
requests, see token burn, bill by tenant. Phase E adds `TraceIdMiddleware` (contextvars
trace_id), `JsonFormatter`, `MetricsMiddleware` (RED), `tracing.log_span` (optional OTel). See
[14](14-web-ui-observability-deploy.md).

**Phase F** (`00ad6e8`) — admin UI + permission-prompt UI + cold/warm sessions. Translating
the previous five gates into "interfaces users can click."

The pattern across the six gates: **each gate is something the teaching version assumes "the
user will handle" but production must enforce.** Teaching code trusts the user; production code
trusts no one.

---

## Step 5: capability expansion (Phases G-J)

With server hardening done, the work shifts to backfilling capabilities the teaching version
had but the framework version did not yet.

**Phase G+H** (`4ef21eb`) — WebSearch / LSP / dynamic workflow + `/agents` enhancements + loop
dispatch. WebSearch lets the agent query the web; LSP lets it understand code symbols; dynamic
workflows let it orchestrate steps at runtime. The toolbox expanded from "filesystem + bash" to
"can query, search, orchestrate." See [05](05-tools.md), [12](12-skills-commands-lsp.md).

**Phase I** (`85e99ef`) — workflow persistence + `/resume` + MCP stdio consumer. Early
workflows were discarded after running; Phase I persists state, supports `/resume`, and adds an
MCP stdio consumer (mini_cc itself as MCP client to external servers). See [11](11-mcp-plugins.md).

**Phase J** (`48b4e68`) — Web UI adapts to new commands (Run Table + Chat merge +
session_resumed). New interaction patterns from capability expansion need UI to follow.

The meaning of Phases G-J: **hardening is not the endpoint — hardening is what lets you add
capabilities safely.** Lay the foundation (A-F) first, then build on top (G-J); each floor
ships independently without collapsing.

---

## Step 6: P5 Docker container sandbox

By this point mini_cc could run, but the only sandbox was `SubprocessSandbox` — running
LLM-generated code directly on the host, naked for untrusted code. P5 (starting at `a2b6386`,
nine commits) introduces the container sandbox.

**ContainerConfig** (`627cb20`) — four-dimension image config (distro/packages/pip/env), from
`sandbox.toml`. **imagebuild** (`df28718`) — renders ContainerConfig into a Dockerfile + wraps
`docker build`. **ContainerRuntime Protocol** (`17d50d5`) — `mini_cc/sandbox/runtime.py`
defines the Protocol, `DockerRuntime` first implementation, `FakeRuntime` test double.
**TenantContainerManager** (`d92115c`) — per-tenant lifecycle + name sanitizer.
**ContainerSandbox** (`1c8af6c`) — `mini_cc/sandbox/container.py` implements the `Sandbox`
Protocol, routes exec/git into the container. **ProjectManager accepts sandbox_factory**
(`2a03c53`) — assembly picks a sandbox per tenant config, default unchanged.
**ServerRuntimeContext + auto-degrade** (`0893a5f`) — docker unavailable → container tenants
softly degrade to subprocess, recorded in `degrades`. **sandbox CLI** (`9c160a4`).

**Cross-platform WSL2 detection** (`44ddd0f`) — on Windows docker usually runs under WSL2;
`osdetect` (`mini_cc/sandbox/osdetect.py`) probes availability + WSL2 backend, adapts
cross-platform command prefixes. Without it, Windows users opening a container sandbox hit an
immediate error.

The key design of P5: **ContainerSandbox implements the `Sandbox` Protocol, same interface as
`SubprocessSandbox`.** Upper-layer AgentLoop and tools have no idea whether code runs locally
or in a container — the payoff of step 1 abstracting both Storage and Sandbox as Protocols. The
full three-tier sandbox architecture is in [03](03-sandbox.md).

---

## Step 7: P6 OpenSandbox three-tier architecture

P5 enabled docker, but docker is not the only option — some environments are better served by
an HTTP-based sandbox service. P6 (starting at `647168c`, tasks 1.1-1.9) introduces
OpenSandbox — an HTTP-API-driven sandbox backend — ultimately producing the three-tier
architecture:

**Tasks 1.1-1.7** (`647168c`..`54e003e`) — `OpenSandboxConfig` + env loader,
`OpenSandboxRuntime._request` + `is_available`, `ensure_running` with tid-metadata dedup,
`status` with state mapping, `exec` via the execd SSE stream, `stop`/`remove`/`list_managed`,
and `build_image` delegating to DockerRuntime. Implemented in
`mini_cc/sandbox/opensandbox_runtime.py`.

**Task 1.8** (`e0fc5de`) — `ServerRuntimeContext` three-tier backend selection.
`_build_runtime()` at `runtime_context.py:57` decides based on `MINI_CC_SANDBOX_BACKEND`:
`opensandbox` preferred, falling back to docker if unset/unavailable; `docker` direct; `auto`
(default) tries opensandbox then docker, returning `None` if neither is present. When `None`,
`_sandbox_factory` at `runtime_context.py:111` auto-degrades tenants marked `container` to
`SubprocessSandbox`, recording the degrade in `self.degrades` (`runtime_context.py:117`).

**MountSpec abstraction** (`3e1d1d6`) — `mini_cc/sandbox/config.py` defines `MountSpec`
(host/PVC/OSSFS); `e551015` makes OpenSandboxRuntime translate it. Makes "mount a directory
into the sandbox" an independent concern, not bound to any backend.

**Stateful code interpreter** (`4596e7f` + `e47d6b1`) — `OpenSandboxInterpreter` adapter +
`/repl` switching via env. Lets mini_cc run a "stateful" Python REPL — variables from one
command are available in the next.

The core of P6: **the three-tier fallback (opensandbox → docker → subprocess) depends on the
Protocol foundation laid in step 1 + step 6.** `ServerRuntimeContext` (see [03](03-sandbox.md))
only swaps the `ContainerRuntime` implementation; everything above is untouched. Adding a new
backend (future K8s/gVisor) is just another `ContainerRuntime` implementation. This is the
long-term value of Protocol-based extension points — every new tier is additive, not rework.

---

## Step 8: Round-2 audit & P0 remediation

By this point mini_cc was functionally complete, but functionally complete is not the same as
production-ready. Commit `5282ff5 docs: land round-2 production audit report` landed a 167-line
audit report (`docs/plans/2026-06-28-production-audit-round2.md`) systematically checking
mini_cc's vulnerabilities under "actually shipping" conditions. The audit split into batches
(batch3-batch7), each fixing one class of issue:

- **batch3** (`d8245ad`) — atomic storage writes + corruption detection + MCP close + killing
  background subprocesses. `FSStorage`'s atomic write (`fs.py:146`) was not fully atomic before
  the audit — a power loss could leave a half-written file.
- **batch4** (`d1ad886`) — SSE queue bound + heartbeat + `Last-Event-Id` replay. An unbounded
  queue causes OOM; without a heartbeat the client cannot tell whether the connection is alive.
- **batch5** (`e3d2295`) — loop run guard + atomic task claim + teammate graceful shutdown.
- **batch6** (`a619f35`) — hook fault isolation + log redaction (`RedactingFilter`) + Redis
  rate limiter.
- **batch7** (`6e08762`) — plan approval timeout + subagent MCP + workflow lock + cron
  catch-up.

Two P0 fixes came directly out of the audit:

**P0-6** (`2595434`) — the subagent's `session_id` was not surfaced in the task tool result.
`core/subagent.py` + `tools/subagent.py` each gained 11 lines, letting the parent agent
retrieve the child agent's session_id to inspect its conversation. Before the audit this
information was lost — subagent debugging was flying blind.

**P0-2** (`83413f9`) — no `cancelled` SSE event was emitted on cancellation. `core/loop.py`
gained 5 lines so the client can distinguish "stream ended normally" from "stream cancelled."

The core lesson: **audits catch what code reviews miss.** Code review asks "is this code
correct"; audits ask "what happens to this system under boundary conditions" — power loss,
OOM, concurrency, timeout, injection. Only after round-2 did mini_cc move from "functionally
complete" to "boundary-condition complete."

---

## Step 9: Workflow V2 W1-W6 + B8

After the audit, feature development resumed. What makes this round special: **every W was a
gap discovered while writing the corresponding chapter.** The original workflow implementation
(`workflow_v2.py` was 385 lines at `f67c73c`, later 732) mixed definition and run; the chapter
author got halfway through and realized "this feature isn't actually implemented," backfilling
as they wrote:

- **W1** (`f67c73c` + `11d798a`) — definition/run separation + storage layer + HTTP routes.
  Originally definition and run were the same object; W1 split them, letting one definition be
  run multiple times. See [10](10-workflow-v2.md).
- **W2** (`8c4f897`) — checkpoint gate resolution API. A checkpoint step pauses for human
  review; W2 added Approve/Reject APIs.
- **W3** (`2c5d2ad`) — inbound webhook resolver. A `webhook_wait` step waits for an external
  webhook; W3 added a receiver endpoint that parses and advances.
- **W4** (`296ef40`) — email subsystem + `email_wait` resolver. Wait for an email before
  advancing; W4 added an email subsystem (IMAP poll + matching).
- **W5** (`f7d2df9`) — validate step. Deterministic state checks — advance only if the
  previous step's output satisfies a condition.
- **W6** (`3901a31` + `15685e6`) — visual editor + drive endpoint + e2e smoke + delete
  definition from the editor. A three-pane editor (definitions/runs | execution timeline | step
  inspector).

**B8** (`aaa5793`) — SSE resume-only path + client auto-reconnect on drop. The final fix for
the time bomb planted in step 2 (P3): the server adds a resume-only path — on
`body.resume=true` it skips the lock + LLM dispatch, replaying only undelivered events from
the per-session event log; the client `sse.ts` adds exponential-backoff reconnect
(1s→2s→4s), with the retry carrying `Last-Event-Id` + `resume: true`. Before the audit,
reconnect would burn tokens twice. See [08](08-sse-streaming.md).

The pattern of W1-W6: **writing documentation is the best gap-finder.** Each chapter forces
the author to walk the corresponding subsystem inside-out, and the "should exist but doesn't"
exposures along the way are W1-W6. This is also why step 10 treats "finishing the 14-chapter
tutorial" as the final production gate.

---

## Step 10: 14-chapter tutorial

Commit `d464d00 docs(mini_cc): 14-chapter bilingual advanced internals tutorial` (this series)
is the final gate of mini_cc's evolution. Fourteen bilingual chapters, each following the same
five-part template (Problem & motivation → Design & implementation → Operation & verification
→ Pitfalls → Summary), with file:line citations, runnable curl/playwright steps, and real
pitfalls.

Why is "writing a tutorial" a production gate? Because **code that runs is not the same as
code that can be understood, and a production-grade framework must be understandable.** A
module-by-module tutorial forces the author to answer: why is this Protocol shaped this way?
Why is this boundary drawn here? Why is this invariant unbreakable? If you cannot answer, the
design is accidental rather than necessary — and accidental designs collapse in the next PR.
W1-W6 were gaps exposed by tutorial writing; B8 was the reconnect double-dispatch exposed
while writing [08](08-sse-streaming.md). The tutorial is the final audit — only the audit
target has shifted from "boundary conditions" to "design soundness."

A byproduct is a **complete ops manual**: each chapter's "Operation & verification" gives
runnable diagnostic steps, "Pitfalls" gives real problems encountered. A new contributor can
get up to speed without reverse-engineering the source — and reverse-engineering is the last
wall between a teaching framework and a production framework a team can adopt.

---

## Invariants across the evolution

Across the ten steps, a great deal of code was rewritten, but three invariants never changed:

1. **Protocol-based extension points.** `Storage` (`storage/base.py:51`), `Sandbox`,
   `ContainerRuntime`, `Tool` are all Protocol + multiple implementations. Every new layer
   (OpenSandbox, stateful REPL, new tools) is another Protocol implementation, leaving upper
   layers untouched. This made step 6 (P5) and step 7 (P6) "additive" rather than "rework."
2. **No module-level global state.** From `76647ac` on, every subsystem instance is
   per-project, and `ProjectManager._assemble()` (`projects/manager.py:278`) wires all
   dependencies at assembly time. This is the precondition for multi-tenant isolation —
   without it, `SessionManager` would have to handle "which project does this lock belong to"
   itself, with exploding complexity.
3. **Tenant isolation from day 1.** `76647ac` established the tenant-scoped directory layout
   (`projects/layout.py`) on day one. If we had done single-tenant first and "added"
   multi-tenancy later, every call site would need retrofitting. This means Phase D scopes and
   the round-2 R2 cross-tenant tests were both "building on the existing foundation."

**Why do these three invariants make every step possible?** Because they separate "extension"
from "modification." Adding OpenSandbox is extension (implementing ContainerRuntime); fixing
SSE resume is modification (touching sse.py + sessions.py). With these three invariants,
extension does not break modification, and modification does not entangle extension. Without
them, at least six of the ten steps would have collapsed.

---

## Lessons learned

1. **Audit before adding features.** `5282ff5` round-2 came after Phases G-J, so batch3-batch7
   fixed a pile of "mines planted while adding features." Had it run right after Phase F,
   W1-W6 would have shipped with atomic writes, SSE boundaries, and rate limiting already in
   place. Lesson: **functional completeness is not production readiness; audits catch boundary
   conditions, not functional correctness.**
2. **SSE resume needs a persistence layer.** B8 (`aaa5793`) fixed reconnect double-dispatch,
   but the key was not the reconnection logic — it was Phase B's (`68b51ba`) prior session
   persistence + per-session event log. The resume-only path is precisely "replay from the
   event log." Without a persistence layer, reconnect either loses events or burns tokens
   twice. Lesson: **stream robustness is built on a persistence layer, not on stream logic.**
3. **Three-tier fallback requires a stable Protocol.** P6's (`e0fc5de`) three-tier selection
   (opensandbox → docker → subprocess) was only possible because P5 (`17d50d5`) had first
   defined the `ContainerRuntime` Protocol. Had P5 hard-coded DockerRuntime, P6's OpenSandbox
   would have required modifying `ServerRuntimeContext` internals. Lesson: **the abstraction
   layer must precede multiple implementations, or the first implementation binds the
   interface.**
4. **Multi-tenant on day 1.** `76647ac` established the tenant-scoped layout on day one, not
   single-tenant first "to add later." Had we done single-tenant first, Phase D scopes and
   the round-2 R2 tests would have required retrofitting every call site. Lesson:
   **multi-tenancy is a foundation, not a feature; foundations are poured on day 1, not
   patched in on day 100.**
5. **Writing documentation is the best gap-finder.** W1-W6 were all "should exist but doesn't"
   discoveries while writing [10](10-workflow-v2.md); B8 was the reconnect double-dispatch
   exposed while writing [08](08-sse-streaming.md). Tutorial writing forces the author to walk
   every subsystem inside-out. Lesson: **deep documentation exposes gaps that code review
   misses; pair it with feature work, not after.**
6. **Soft degradation beats hard failure.** `0893a5f`'s auto-degrade lets container tenants
   fall back to subprocess when docker is unavailable instead of erroring;
   `runtime_context.py:117` records the degrade in `degrades` so it is observable. Teaching
   code tends to "error on misconfiguration"; production code tends to "use the next-best
   option + log." Lesson: **replace "hard failure" with "soft degrade + observable," or a
   single point of unavailability bricks the entire service.**

---

## Summary

mini_cc's evolution is a chain of "teaching → framework → hardening → expansion → audit →
documentation": `c6a27ef`'s 11 sessions teach the concepts → `76647ac` raises the skeleton
(Protocol + multi-tenancy + no global state) → P3/P4 cross the network boundary → Phases A-F
pass six production gates → Phases G-J safely backfill capabilities → P5/P6 stack up the
three-tier sandbox → round-2 audit backfills boundary conditions → W1-W6 + B8 backfill gaps
while writing the tutorial → `d464d00` uses the 14-chapter tutorial to turn "runs" into
"understood and adoptable." No step is isolated — the Protocol foundation makes the three-tier
sandbox additive rather than rework; the persistence layer makes SSE resume possible;
multi-tenancy on day 1 lets every later isolation feature grow on the foundation.

These 14 chapters are mini_cc *as it stands today*, dissected module by module. After reading
this chapter you should know the "why it was needed" and "what it replaced" behind each
chapter; go back to [01 overview](01-overview.md) and walk through 01-14 again, and you will
see not just "the framework looks like this" but "why the framework looks like this."

---

[ < [14](14-web-ui-observability-deploy.md) ] [ [README](../README.md) ] · [中文版本](../zh/15-evolution-migration.md)
