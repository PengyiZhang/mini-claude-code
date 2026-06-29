# 04 — The Agent Loop

[ < prev ] [ next > ] · [中文版本](../zh/04-agent-loop.md)

## Problem & motivation

An "agent" is not a single LLM call; it is a **turn-based driving loop** that keeps
issuing model requests, executing the tools the model asks for, and feeding the
results back until the model signals completion. `mini_cc` exposes that loop as a
generator so the same code path serves three very different consumers at once:

1. **HTTP/SSE clients** consume the yielded events as a live stream.
2. **SDK embedders** iterate the generator directly inside their own process.
3. **Sub-agents** (`task` tool, workflow dispatch) re-enter the loop with a
   restricted toolset.

A single loop instance must therefore be safe to drive from multiple callers, must
survive server crashes mid-turn, and must keep token usage bounded when a
long-running session grows past the context window. `mini_cc/core/loop.py` is
where all of these concerns land. This chapter walks through the loop's anatomy:
the per-turn event contract, the per-session re-entrancy lock, the layered
compaction trigger, error recovery (`MAX_RECOVERY_RETRIES`, model fallback,
`output_style`), and how sub-agents are spawned as fresh child loops.

## Design & principles

### The turn contract

`AgentLoop.run(user_input)` is a generator that yields a strictly-typed stream of
events. The full taxonomy is documented at the top of `mini_cc/core/loop.py:4-9`:

```
{"type": "text", "text": str}                       # streamed assistant token
{"type": "tool_use", "name", "input", "id"}         # model wants to call a tool
{"type": "tool_result", "tool_use_id", "content"}   # tool returned
{"type": "permission_request", "request_id", ...}   # interactive gate (ch 06)
{"type": "todos_updated", "todos": [...]}           # board state changed
{"type": "cron_fired" / "background_notification"}  # scheduler injections
{"type": "retry", "reason": "429"|"529", "attempt"} # backoff in progress
{"type": "max_tokens_escalation", "max_tokens"}     # grew the budget
{"type": "cancelled"}                               # user hit stop mid-turn
{"type": "done"}                                    # turn finished cleanly
{"type": "error", "message"}                        # unrecoverable failure
```

Consumers should treat `done` / `error` / `cancelled` as terminal. The loop
guarantees exactly one terminal event per `run()` invocation.

### The per-iteration cycle

```
   ┌──────────────────────────────────────────────────────────────┐
   │  AgentLoop._run_impl()  — one user turn = N iterations       │
   │                                                              │
   │  while not _stop.is_set():                                   │
   │    1. inject cron_fired + background notifications           │
   │    2. nudge todos if no todo_write in 3 rounds               │
   │    3. _refresh_tools()  ← MCP tools can appear live          │
   │    4. prepare_context()  ← layered compaction (see below)    │
   │    5. assemble system prompt (or use override)               │
   │    6. open provider stream (retry 429/529 with backoff)      │
   │    7. forward text_delta events live                         │
   │    8. on message_stop: append assistant blocks               │
   │    9. if tool_use: dispatch, collect results, loop again     │
   │       else: yield done + persist + return                    │
   └──────────────────────────────────────────────────────────────┘
```

The cycle is driven by the model's `stop_reason`. `tool_use` re-enters the loop
with the tool results appended as a new user turn; `end_turn` exits. The
`max_tokens` path escalates once (`DEFAULT_MAX_TOKENS` → `ESCALATED_MAX_TOKENS`,
`mini_cc/config.py:27-28`) and then, if it still hits the cap, sends a
`CONTINUATION_PROMPT` up to `MAX_RECOVERY_RETRIES` times before giving up
(`mini_cc/core/loop.py:681-695`).

### Per-session re-entrancy lock

Two threads entering `run()` on the same loop would race on `self.messages` and
corrupt the transcript. The guard is explicit:

```python
# mini_cc/core/loop.py:424-430
with self._running_lock:
    if self._running:
        raise RuntimeError(
            "AgentLoop.run() already in progress on this loop — "
            "concurrent calls would corrupt the transcript. ...")
    self._running = True
```

The HTTP/SessionManager path serializes per-project already, but SDK embedders
who spin their own threads hit this fail-fast. Use a separate `AgentLoop` per
parallel session, never two threads on one loop.

### Layered compaction

`prepare_context()` (`mini_cc/core/compaction.py:129`) applies three passes in
order, each progressively more destructive:

| Pass | What it does | Trigger |
|------|--------------|---------|
| `tool_result_budget` | Truncate tool_result bodies older than the 3 most recent to 2000 chars | always |
| `snip_compact` | Replace the oldest tool-bearing assistant turn with a placeholder | only when over `CONTEXT_LIMIT` (50k tokens) |
| `micro_compact` | Collapse trailing empty user messages | always |
| `compact_history` | Keep last 6 messages, summarize the rest | only when still over budget |

A reactive fallback also fires when the provider returns a "prompt too long"
error: `compact_history` runs once (guarded by `has_attempted_reactive_compact`)
and the iteration retries (`mini_cc/core/loop.py:632-636`). Before any destructive
compaction, `_save_transcript` writes the full pre-compaction message list so the
conversation can be replayed.

### Crash recovery & transcript repair

State is persisted after every turn via `_persist()`. On warm-load,
`repair_dangling_tool_uses()` (`mini_cc/core/loop.py:93`) fixes two corruption
shapes that otherwise make the Anthropic API return 400:

- **Forward dangling**: tail is an assistant message with `tool_use` blocks but
  no matching `tool_result` → append a synthetic user turn marking each as
  `"[interrupted by server restart]"`.
- **Reverse orphan** (P1-9): tail is a user message whose `tool_result` blocks
  reference `tool_use_id`s absent from the prior assistant turn → strip the
  orphans, or replace the whole message with a text note if all were orphans.

The same partial-persist logic runs on cancel and on mid-stream exceptions
(`loop.py:597-664`): whatever text + tool_use the model produced before the
interruption is saved as a coherent assistant message, with matching
`[interrupted]` tool_results synthesized so the next turn's API call sees
balanced pairs.

`RecoveryState` (`mini_cc/core/recovery.py:14`) tracks `recovery_count`,
`consecutive_529`, `has_escalated`, `has_attempted_reactive_compact`,
`current_model`, and `output_style` (the verbosity hint set by `/output-style`,
read by the system-prompt builder).

### Sub-agent spawning

`spawn_subagent()` (`mini_cc/core/subagent.py:36`) builds a fresh `AgentLoop`
with a frozen, restricted toolset — only `bash`, `read_file`, `write_file`,
`edit_file`, `glob`, `grep` (`SUBAGENT_TOOL_NAMES`, line 23). The sub-loop runs
to completion with its own `subagent-<uuid8>` session_id, a hard cap of 30 tool
calls, and a focused system prompt that ends with "Do not spawn more agents."
MCP tools are excluded unless the caller passes `allow_mcp=True` (B10). The
parent loop's `on_event` is forwarded so sub-agent `tool_use`/`tool_result`
activities surface live in the parent's SSE stream.

## Operation & configuration

### Environment variables (read by `AnthropicConfig.from_env`, `mini_cc/config.py:109`)

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` / `MINI_CC_ANTHROPIC_API_KEY` | Anthropic SDK credential |
| `ANTHROPIC_BASE_URL` / `MINI_CC_ANTHROPIC_BASE_URL` | Anthropic-compatible endpoint |
| `MODEL_ID` | Primary model (default `claude-sonnet-4-6`) |
| `FALLBACK_MODEL_ID` | Switched to after 2 consecutive 529s |
| `LITELLM_API_KEY` / `OPENAI_API_KEY` | litellm credential (model prefix routes here) |
| `LITELLM_BASE_URL` / `OPENAI_BASE_URL` | OpenAI-compatible endpoint |
| `TAVILY_API_KEY` | Enables Tavily-backed `web_search` |
| `MINI_CC_MCP_SERVERS` | JSON map of MCP servers to auto-connect |

### Loop tuning constants (`mini_cc/config.py:27-35`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `DEFAULT_MAX_TOKENS` | 8000 | Per-request output ceiling |
| `ESCALATED_MAX_TOKENS` | 16000 | After first `max_tokens` stop_reason |
| `MAX_RETRIES` | 3 | Stream-open retries on 429/529 |
| `MAX_RECOVERY_RETRIES` | 2 | Continuation prompts after escalation |
| `CONTEXT_LIMIT` | 50000 | Token estimate that triggers compaction |
| `KEEP_RECENT_TOOL_RESULTS` | 3 | Tool results kept full before truncation |

### Provider selection

`select_provider(model, cfg)` (`mini_cc/core/llm.py:430`) routes by model-name
prefix. `claude-*` (no slash) → `AnthropicProvider`; any prefix in
`LITELLM_PREFIXES` (`openai/`, `deepseek/`, `qwen/`, `gemini/`, ...) →
`LiteLLMProvider`. Both yield the same normalized `StreamEvent` shape
(`llm.py:38`), so `loop.py` never branches on backend.

## Verification steps

Run against the backend on `:8002`. Replace `$TID`, `$PID`, `$SID`, `$KEY` with
your tenant/project/session/api-key values.

```bash
# 1. Open an SSE stream for a session
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -H "Content-Type: application/json" \
  -d '{"message":"List the files in the project root using bash."}'

# Watch for: text deltas → tool_use(bash) → tool_result → done

# 2. Trigger max_tokens escalation by asking for a very long output
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"Write 5000 words about ocean currents."}'
# Expect a {"type":"max_tokens_escalation","max_tokens":16000} event

# 3. Inspect persisted transcript after a crash-simulating restart
ls $WORKSPACE/.mini_cc/sessions/$PID/$SID/
# messages.json + todos.json should be present and JSON-valid

# 4. Drive a sub-agent via the task tool
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"Use the task tool to grep for TODO in src/ and summarize."}'
# Sub-agent tool_use/tool_result events should appear nested under the parent turn
```

## Common pitfalls & debugging

1. **`RuntimeError: AgentLoop.run() already in progress`** — two threads share
   one loop. The fix is per-session loops, not a coarser lock. SDK embedders
   who queue user messages on a worker thread are the usual culprit.
2. **`400` on resume after a crash** — the warm-load repair didn't run. Confirm
   `repair_dangling_tool_uses()` is invoked by your session warm-up path; the
   symptom is an orphan `tool_result` block in `messages.json`.
3. **Stuck re-looping on `max_tokens`** — escalation fires once, then
   `MAX_RECOVERY_RETRIES=2` continuation prompts. If the model still produces
   over-long output, the loop yields `done` with truncated content. Raise
   `ESCALATED_MAX_TOKENS` or split the task.
4. **Compaction discards tool output too eagerly** — `tool_result_budget`
   truncates anything older than the 3 most recent tool results to 2000 chars.
   If the agent needs older output, ask it to re-run the tool rather than
   raising `KEEP_RECENT_TOOL_RESULTS`.
5. **429/529 storms** — the SDK retries twice internally; the loop adds its own
   `MAX_RETRIES=3` backoff on stream open. After 2 consecutive 529s it switches
   to `FALLBACK_MODEL_ID` if set. Without a fallback, exhaustion raises and the
   turn ends with an `error` event.

## Further reading

- Source: `mini_cc/core/loop.py`, `mini_cc/core/recovery.py`,
  `mini_cc/core/compaction.py`, `mini_cc/core/llm.py`, `mini_cc/core/subagent.py`
- Sibling chapters: [05 — Tools](../en/05-tools.md),
  [06 — Permissions](../en/06-permissions.md)
- For the conceptual framing of an agent turn, see the `sNN-*` series under
  `docs/{en,zh}/`.
