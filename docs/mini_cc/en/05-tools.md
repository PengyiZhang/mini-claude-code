# 05 — Tool Registry & Built-ins

[ < prev ] [ next > ] · [中文版本](../zh/05-tools.md)

## Problem & motivation

The model in an agent loop is useless without **tools** — the verbs it can
invoke to read files, run commands, fetch URLs, manage todos, and so on. A
backend-integrable framework needs the tool layer to be three things at once:

1. **Uniform** — every tool shares one shape so the model, the dispatcher, and
   the API schema renderer can all treat them generically.
2. **Context-bound** — a tool handler must close over the per-invocation state
   (which sandbox, which storage, which session) without global variables, so
   multi-tenant deployments stay isolated.
3. **Extensible** — new tools (MCP servers, project-specific verbs) must slot
   in without touching the loop.

`mini_cc/tools/` is that layer. This chapter covers the core abstraction
(`Tool`, `ToolContext`, `FunctionTool`), the registry/dispatch/render pipeline
(`builtin_tools`, `dispatch`, `to_anthropic`), and then groups the ~45 built-in
tools by purpose so you can find what you need without reading every file.

## Design & principles

### The Tool contract

Every tool implements the `Tool` protocol (`mini_cc/tools/base.py:62`):

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict
    def handle(self, ctx: ToolContext, args: dict) -> str: ...
```

Three fields the model sees (`name`, `description`, `input_schema`) plus one
method that returns a string. **Tools always return strings** — never raise,
never return structured data. Errors are encoded as `"Error: ..."` so the model
can react and recover. The convenience wrapper `FunctionTool` (line 70) turns
any `(ctx, args) -> str` callable into a Tool and wraps it in a try/except that
converts exceptions into `Error:` strings.

### ToolContext — per-invocation state

`ToolContext` (`base.py:22`) is a dataclass the loop builds fresh for each
`_execute_tool_calls` run. It carries everything a handler might need without
reaching into globals:

```
ToolContext
├── project_id, session_id          # identity
├── sandbox: Sandbox                # FS + bash boundary (ch 04)
├── storage: Storage                # persistence
├── todos: list[dict]               # live task board
├── mark_todos_updated()            # callback to persist + emit event
├── skills_loader, memory_loader    # lazy catalogs
├── scheduler (cron), wakeups       # scheduling
├── mcp_pool                        # connected MCP servers
├── teams (TeammateSpawner)         # multi-agent message bus
├── project_ref                     # back-reference for spawning sub-agents
├── background_scheduler            # long-running task pool
├── background_tools                # name->Tool map for bg worker re-entry
├── cancel_event                    # signal to kill a bg subprocess
└── on_subagent_event               # sink for nested-loop events (task tool)
```

Because `ToolContext` is a slable dataclass, the loop attaches
`ctx.workflow_dispatch` at build time (`loop.py:790`) so workflow tools can
spawn focused sub-agents without a circular import.

### Registry, dispatch, render

Three functions in `mini_cc/tools/__init__.py` form the pipeline:

| Function | Line | Purpose |
|----------|------|---------|
| `builtin_tools()` | 27 | Returns the full ordered list of built-in tools (FS, bash, todo, skills, memory, cron, mcp, task, worktree, teams, subagent, web, bgtask, websearch, wakeup, workflow, repl, LSP) |
| `dispatch(tools)` | 47 | Builds `{name: Tool}` lookup used by the loop's `_handlers` |
| `to_anthropic(tools)` | 38 | Renders tools as Anthropic API `input_schema` definitions for the model request |

The loop rebuilds the tool pool every iteration (`_refresh_tools`,
`loop.py:278`) unless the caller froze it — that is how newly-connected MCP
tools appear live mid-session. MCP tools are namespaced `mcp__<server>__<tool>`
and merged in via `MCPPool.all_tools()`.

### How a tool call flows through the loop

```
   model emits tool_use block
            │
            ▼
   AgentLoop._execute_tool_calls          (loop.py:793)
            │
            ├── yield {"type":"tool_use", ...}        ← announce
            │
            ├── [if name in prompt_tools]            ← interactive gate
            │     PermissionInterceptor.create/wait  (ch 06)
            │
            ├── hooks.trigger(PreToolUse, name, input) ← deny returns a string
            │
            ├── [if should_run_background]           ← slow bash offload
            │     BackgroundScheduler.start(...)
            │
            └── handlers[name].handle(ctx, input)    ← the actual call
                    │
                    └── returns string
            │
            ├── drain subagent_events (task tool)
            │
            └── yield {"type":"tool_result", ...}
```

The dispatcher never imports tool implementations directly — it goes through
`self._handlers[name]`, which is just a dict. "Unknown tool" becomes a plain
string result so the model can retry with a different name.

One departure from the strictly serial flow above: consecutive read-only
tools flagged `parallel_safe` in a single assistant turn run concurrently in a
small thread pool. The `tool_use` events for the whole batch are still emitted
first, and the `tool_result` events follow in completion order; a serial tool
after the batch waits for it. The pool size comes from
`MINI_CC_TOOL_WORKERS` (default 4; below 2 means serial).

### Built-in tool categories

The ~45 built-ins group into nine families. File and bash operations are the
foundation; everything else layers on top.

**1. Filesystem** (`tools/fs.py`) — `read_file`, `write_file`, `edit_file`,
`glob`, `grep`. Every operation routes through `ctx.sandbox`; direct `open()`
would bypass the boundary. `grep` supports `output_mode` of
`files_with_matches` / `content` / `count`.

**2. Shell execution** (`tools/bash.py`) — `bash`. Per-call `timeout` (hard cap
600s), optional `cwd` (validated inside project root), and
`run_in_background=true` which hands off to the project's
`BackgroundScheduler`. Output is truncated at 50KB. The bash tool prefers a
real bash on PATH (Git Bash on Windows) so Unix syntax works everywhere.

**3. Background tasks** (`tools/background.py`, `tools/bgtask.py`) —
`task_output`, `task_stop`. The scheduler auto-detects slow operations
(`is_slow_operation` matches `install`, `build`, `test`, `deploy`, `pytest`,
etc.) and offloads them; results land as `<task_notification>` injected into a
later turn. The explicit `run_in_background` flag forces offload regardless.

**4. Web** (`tools/web.py`, `tools/websearch.py`) — `web_fetch` (urllib,
50KB cap, 20s default timeout) and `web_search` (Tavily when
`TAVILY_API_KEY` is set, else a clear "not configured" error so the agent
falls back to `web_fetch`).

**5. Planning & memory** (`tools/todo.py`, `tools/memory.py`,
`tools/skills.py`) — `todo_write` (persists to `ctx.todos`, emits
`todos_updated`); `memory_write`/`memory_recall`/`memory_list`;
`load_skill`. The loop injects a `<reminder>Update your todos.</reminder>`
nudge after 3 rounds without a `todo_write`.

**6. Sub-agents & teams** (`tools/subagent.py`, `tools/task.py`,
`tools/teams.py`) — the `task` tool spawns a focused sub-agent via
`spawn_subagent` (ch 04) with a frozen filesystem+bash toolset. Teams tools
(`send_message`, `check_inbox`, `spawn_teammate`, `submit_plan`,
`request_plan`, `review_plan`, `list_teammates`, `request_shutdown`) route
through `ctx.teams` (TeammateSpawner) which owns a MessageBus. Task-system
tools (`create_task`, `list_tasks`, `get_task`, `claim_task`,
`complete_task`) manage durable work items with dependency gating.

**7. Scheduling** (`tools/cron.py`, `tools/wakeup.py`) — `schedule_cron` /
`list_crons` / `cancel_cron` (5-field cron, durable across restarts) versus
`schedule_wakeup` / `list_wakeups` / `cancel_wakeup` (second-precision,
in-memory only, capped at 3600s — use cron for longer delays). The loop ticks
the scheduler each iteration and injects fired prompts as user messages.

**8. Code execution** (`tools/repl.py`) — `execute_code` for `python`
(stateful: variables pickled per-session under `.mini_cc/repl/`), `javascript`
and `shell` (stateless one-shots). Routes through `ctx.sandbox.execute()` so
the container sandbox gets the feature transparently.

**9. Worktree & workflow** (`tools/worktree.py`, `tools/workflow.py`) —
`create_worktree`/`keep_worktree`/`remove_worktree` for isolated git worktrees;
`workflow_create`/`workflow_add_step`/`workflow_run_step`/`workflow_run_all`/
`workflow_status`/`workflow_set_state` for declared multi-step plans that
dispatch through `ctx.workflow_dispatch` (a sub-agent per step).

Plus `connect_mcp` (`tools/mcp.py`) and the LSP tool — the integration verbs.

### Adding a custom tool

```python
from mini_cc.tools import FunctionTool, ToolContext, builtin_tools

def _my_tool(ctx: ToolContext, args: dict) -> str:
    return f"hello {args.get('name')}"

MY_TOOL = FunctionTool(
    name="hello",
    description="Say hello.",
    input_schema={"type": "object",
                  "properties": {"name": {"type": "string"}},
                  "required": ["name"]},
    fn=_my_tool,
)

# Register when building the project:
tools = builtin_tools() + [MY_TOOL]
loop = AgentLoop(project, session_id, tools=tools, ...)
```

Passing a frozen `tools` list disables per-iteration refresh — acceptable when
you control the full toolset.

## Operation & configuration

### Tool-relevant environment variables

| Var | Tools affected |
|-----|----------------|
| `TAVILY_API_KEY` | `web_search` (required for Tavily) |
| `MINI_CC_MCP_SERVERS` | MCP servers auto-connected at project load; their tools appear as `mcp__*` |
| `MINI_CC_TOOL_WORKERS` | Thread-pool size for `parallel_safe` tool batches (default 4; below 2 = serial) |
| `ANTHROPIC_API_KEY` / `MODEL_ID` | Determines whether `connect_mcp` and tool calls can be served at all |

### Per-tool tunables (hardcoded, see source)

| Constant | File | Default |
|----------|------|---------|
| `MAX_TIMEOUT_SECONDS` | `tools/bash.py:21` | 600 |
| `DEFAULT_TIMEOUT_SECONDS` | `tools/bash.py:22` | 120 |
| `MAX_BYTES` | `tools/web.py:16` | 50,000 |
| `DEFAULT_TIMEOUT` (web) | `tools/web.py:17` | 20 |
| `MAX_DELAY_SECONDS` | `tools/wakeup.py:20` | 3600 |
| `SLOW_KEYWORDS` | `tools/background.py:26` | install, build, test, deploy, ... |

### Observability endpoints

Tools surface through the SSE event stream (ch 04). The `tool_use` and
`tool_result` events carry `name`, `input`, `id`, and `content`. The
`background_notification` event signals a background task completed; pair with
`task_output` to fetch the result.

## Verification steps

```bash
# 1. List what tools the model can see (via a session send that asks)
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"Call read_file on package.json and summarize."}'
# Expect: tool_use(read_file) → tool_result → text summary → done

# 2. Test bash with a background offload
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"Run: npm install. Use run_in_background if needed."}'
# A slow keyword triggers BackgroundScheduler; bg_id returned, result in next turn

# 3. Verify a custom tool appears in the rendered schema
python -c "from mini_cc.tools import builtin_tools, to_anthropic; \
  import json; print(json.dumps([t['name'] for t in to_anthropic(builtin_tools())]))"

# 4. MCP round-trip: connect then call an mcp__ tool
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"connect_mcp name=docs, then list what tools docs exposes."}'
```

## Common pitfalls & debugging

1. **`Unknown tool: <name>`** — the dispatcher dict lookup missed. Either the
   tool list was frozen before MCP connected, or the name has a typo. MCP tools
   are `mcp__<server>__<tool>` (double underscore), not single.
2. **Tool returns `Error: ...` but the model ignores it** — that is by design;
   the model is expected to read the error string and retry. If it loops, the
   error content is the leverage point — make it actionable.
3. **Background task result never arrives** — `BackgroundScheduler` is per-
   project; if `ctx.background_scheduler` is None the call falls back to
   synchronous execution with no notification. Check that the project was built
   with a scheduler attached.
4. **`run_in_background=true` silently runs sync** — same root cause: no
   `BackgroundScheduler`. The bash tool returns an explicit error string in
   that case (`tools/bash.py:42`).
5. **Custom tool exceptions vanish** — `FunctionTool.handle` catches all
   exceptions and returns `"Error: <Type>: <msg>"`. To get a stack trace,
   register a `PostToolUse` audit hook (ch 06) or attach a logger inside your
   handler before raising.

## Further reading

- Source: `mini_cc/tools/base.py`, `mini_cc/tools/__init__.py`, and the
  per-category modules under `mini_cc/tools/`.
- Sibling chapters: [04 — Agent Loop](../en/04-agent-loop.md),
  [06 — Permissions](../en/06-permissions.md)
- For sandbox internals (where bash/file ops actually execute), see the
  `mini_cc/sandbox/` package and the `sNN-*` conceptual series.
