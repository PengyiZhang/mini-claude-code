[ < [12](12-skills-commands-lsp.md) ] [ [14](14-web-ui-observability-deploy.md) > ] · [中文版本](../zh/13-teams-scheduler.md)

# 13 — Agent Teams & Scheduler

> Advanced internals, chapter 13. This chapter widens the "one AgentLoop drives one session serially" model into three asynchronous execution primitives: **multi-agent teams**, **second-resolution one-shot wakeups**, **minute-resolution cron**, plus the matching **background task pool**. By the end you should be able to explain the thread ownership, state-isolation boundary, and protocol timing of each async path.

---

## Problem & motivation

The core `AgentLoop` (`mini_cc/core/loop.py`) is a single-threaded, blocking main loop. But real workflows break the "do one thing at a time" assumption in at least four ways:

1. **Parallel subtasks** — the lead agent fans out "refactor + write tests + update docs" to three teammates running in parallel and only merges results.
2. **Self-pacing** — wait for a 5-minute build, poll CI, run a slow `/loop` iteration. Need "wake myself in N seconds" without blocking the current thread.
3. **Recurring triggers** — daily 9am status report, every-10-min external sync. Need durability and catch-up across restarts.
4. **Slow-command offload** — `npm install` / `docker build` shouldn't pin the main loop, but their result must re-enter the model context next turn as a `task_notification`.

These four require three distinct execution primitives: threads (teammates), timers (wakeup/cron), and background workers. They share the same Storage but **state must stay isolated** — a teammate crash must not take down the main loop, a misconfigured wakeup must not flood the inbox. This chapter unpacks each.

---

## Design & principles

### Overview: thread ownership of the three async paths

```
┌──────── HTTP request thread (FastAPI worker) ──────────┐
│   POST /send → AgentLoop.run(prompt), sync iteration   │
│        │                                               │
│        ├─ _inject_cron_fired()           # drain cron   │
│        ├─ _inject_background_notifications()  # drain bg │
│        └─ should_run_background()? → BackgroundScheduler.start()
│                                   └→ new daemon thread  │
└────────────────────────────────────────────────────────┘
              │ spawn_teammate
              ▼
┌──────── daemon thread: teammate:<name> ───────────────┐
│   sub-AgentLoop(f"teammate-{name}").run(identity+prompt)│
│   loop: run turn → check inbox → idle poll → next turn │
└────────────────────────────────────────────────────────┘

┌─── In-process schedulers (no own thread; tick driven by loop) ──┐
│   CronScheduler.tick()   ← top of each main-loop iteration      │
│   WakeupScheduler.tick() ← top of each main-loop iteration      │
│   BackgroundScheduler    ← worker threads, self-write-back      │
└─────────────────────────────────────────────────────────────────┘
```

Key point: cron and wakeup **have no polling thread of their own**. Their `tick()` is invoked at the top of each main `AgentLoop` iteration (`mini_cc/core/loop.py:733-745`), and fired items are injected as a `[Scheduled] ...` user message into the current conversation. This means **when no AgentLoop is running, cron does not fire on its own** — an intentional simplification (see the TODO note in `mini_cc/scheduler/cron.py:6-9`). If you need session-less cron, you have to add a shared ticker thread.

### Teammate model: each teammate = its own AgentLoop + session

`TeammateSpawner` (`mini_cc/teams/__init__.py:147`) is a project-scoped registry. `spawn()` (`teams/__init__.py:188`) starts a daemon thread whose body `_runner()` (`teams/__init__.py:285`) builds a **brand-new sub-AgentLoop** via `loop_factory`:

```python
# mini_cc/teams/__init__.py:287
loop = self._loop_factory(f"teammate-{info.name}")
identity = (f"<identity>You are '{info.name}', a {info.role}. "
            f"Use tools to complete the requested work. "
            f"Send your final summary to 'lead' via send_message "
            f"before stopping. After calling submit_plan, end "
            f"your turn and wait for approval.</identity>")
```

The session id has the shape `teammate-<name>`; that prefix does two things:

1. **Sender identification in tools/teams.py** — `_name_from_session()` (`tools/teams.py:18`) strips the `teammate-` prefix to recover the teammate name; the lead's session has no such prefix and is therefore identified as `lead`.
2. **Recursion guard** — `_spawn()` (`tools/teams.py:78`) refuses to spawn from inside a teammate context, preventing unbounded fan-out.

Each teammate gets its own sandbox (`projects/manager.py:_build_teammate_loop`), so when `set_worktree()` redirects a teammate's sandbox root to a worktree path, **it does not pollute other sessions** (`teams/__init__.py:425`).

### Message bus: append-only JSONL inbox

`MessageBus` (`teams/__init__.py:37`) uses `<workspace>/.mailboxes/<name>.jsonl` as a mailbox. Three primitives:

| Method | Behavior |
|---|---|
| `send(from, to, content, msg_type)` | Append one JSON line to `to.jsonl` |
| `read_inbox(agent)` | **Drain** (read then delete) |
| `peek_inbox(agent)` | Read-only, used by idle poll and `/agents inbox` |

"Read-then-delete" is intentional: each message is consumed exactly once, so a teammate cannot re-read the same plan_approval_response.

### Plan-approval protocol: between-turn sync gate

```
teammate turn N: calls submit_plan(plan)
     ↓
ProtocolTracker.register(ProtocolState(type="plan_approval"))
     ↓ bus.send → lead.jsonl: plan_approval_request
teammate _wait_for_plan_verdict(): block-polling inbox
     ↓                                  ↑
     │ (10-min timeout, plan_approval_timeout)│
     ↓                                  │
lead turn: review_plan(request_id, approve=True)
     ↓ bus.send → teammate.jsonl: plan_approval_response
     ↓
teammate gets verdict → next_input = "[Plan approved]" → enters turn N+1
```

Note that plan-approval is a **between-turn gate**, not a mid-turn gate (`teams/__init__.py:11-14`): after `submit_plan` the model is instructed to end its turn, and the spawner blocks before the next turn starts. This avoids the complexity of interrupting an LLM stream mid-generation. `plan_approval_timeout` defaults to 600 s (`teams/__init__.py:177`) — long enough for a human review during work hours, short enough that a dead lead doesn't lock the teammate forever.

### Wakeup: second-resolution, in-memory, one-shot

`WakeupScheduler` (`scheduler/wakeup.py:29`) is purely in-memory. `schedule()` (`wakeup.py:40`) computes `fire_at` from `time.monotonic()` plus the delay; `tick()` (`wakeup.py:67`) returns and removes all wakeups whose deadline has passed.

Design constraints (see `tools/wakeup.py:17-20`, `wakeup.py:1-9`):

- **Delay cap 3600 s** (1 hour). Longer belongs to cron.
- **No persistence across restarts.** If the process dies, pending wakeups vanish — intentional; durability is cron's job.
- **Each AgentLoop owns its own instance** (comment at `wakeup.py:30-33`), not thread-safe across loops.

Tool entries: `schedule_wakeup` (`tools/wakeup.py:95`), `list_wakeups`, `cancel_wakeup`.

### Cron: minute-resolution, durable, catches up across restarts

`CronScheduler` (`scheduler/cron.py:102`) supports standard 5-field cron (`min hour dom month dow`, POSIX semantics, DOW 0=Sun). Two design choices matter:

1. **Persistence** — jobs with `durable=True` are saved via `storage.save_cron()` (`cron.py:133-135`).
2. **B12 catch-up window** — after a restart, `_catch_up_missed_locked()` (`cron.py:196`) scans back up to 24 h (`MAX_CATCHUP_WINDOW_SECONDS`, `cron.py:108`) and **fires each job at most once** (the most recent matching minute in the window), so a long outage doesn't enqueue a flood of duplicate fires.

POSIX DOM/DOW "OR" semantics are implemented in `cron_matches()` (`cron.py:48-54`): if both are non-`*`, either matching triggers the fire.

### Background tasks: the `should_run_background` gate + cooperative cancel

`should_run_background()` (`tools/background.py:38`) is the single enqueue criterion:

```python
# mini_cc/tools/background.py:38-42
def should_run_background(tool_name, tool_input):
    if tool_name != "bash":
        return False
    return bool((tool_input or {}).get("run_in_background")) \
        or is_slow_operation(tool_name, tool_input)
```

`is_slow_operation` does a coarse keyword match on the bash command (`install|build|test|deploy|compile|...`, `background.py:26-28`). On a hit, `BackgroundScheduler.start()` (`background.py:70`) spawns a daemon thread running the handler; the main loop immediately returns `[Background task bg_xxxxxxxx started]`. When the worker finishes, the next `collect_notifications()` (`background.py:199`) wraps its result in `<task_notification>` XML and injects it into the conversation.

`stop()` (`background.py:159`) is **cooperative cancellation**: it sets the task's `cancel_event`, which `sandbox.execute()` polls every 100 ms; on signal it SIGTERMs the subprocess. Python threads themselves are not hard-killable.

---

## Operation & configuration

### Teams tools

| Tool | Args | Who can call | Source |
|---|---|---|---|
| `spawn_teammate` | `name, prompt, role?` | lead only (recursion gate) | `tools/teams.py:166` |
| `send_message` | `to, content, msg_type?` | lead + teammate | `tools/teams.py:126` |
| `check_inbox` | (none) | lead + teammate | `tools/teams.py:141` |
| `list_teammates` | (none) | lead | `tools/teams.py:148` |
| `request_shutdown` | `name` | lead | `tools/teams.py:155` |
| `submit_plan` | `plan` | teammate only | `tools/teams.py:181` |
| `review_plan` | `request_id, approve, feedback?` | lead | `tools/teams.py:207` |

### Slash commands

| Command | Effect | Source |
|---|---|---|
| `/agents` | List all teammates (alive + recently stopped), with role, age, inbox count | `commands/registry.py:560` |
| `/agents stop <name>` | Send shutdown_request to a teammate | `commands/registry.py:579` |
| `/agents inbox <name>` | **Peek** (no consume) a teammate's inbox | `commands/registry.py:589` |
| `/loop` | List cron + wakeup jobs | `commands/registry.py:716` |
| `/loop cancel <id>` | Cancel a cron or wakeup job | `commands/registry.py:739` |
| `/bg` | List background tasks | `commands/registry.py:1051` |
| `/bg stop <bg_id>` | Cancel a background task | `commands/registry.py:1063` |

### Scheduler config (constructor params)

| Param | Default | Notes |
|---|---|---|
| `idle_poll_interval` | 5.0 s | teammate idle-poll interval for inbox |
| `idle_timeout` | 60.0 s | how long a teammate idles before exiting |
| `plan_approval_timeout` | 600.0 s | how long a teammate waits for plan verdict |
| `MAX_DELAY_SECONDS` (wakeup) | 3600 | hard cap on the wakeup tool |
| `MAX_CATCHUP_WINDOW_SECONDS` (cron) | 86400 | cron catch-up window after restart |

> These are **not env-configurable today** — they're Python constants on `TeammateSpawner.__init__` (`teams/__init__.py:160-177`) and `tools/wakeup.py:20`. Changing them requires editing source or passing overrides at spawner construction (`projects/manager.py:317`).

---

## Verification steps

Assume the backend runs on `:8002` (the repo's e2e default) and a tenant key exists (`python -m mini_cc.server keygen demo`).

```bash
# 1. Start the backend
python -m mini_cc.server  # listens on :8000; e2e uses :8002

# 2. Prereqs: create tenant key + project + session (see ch 7 for HTTP server)

# 3. Verify spawn + convergence: lead spawns a teammate that replies and stops
curl -N :8002/tenants/demo/projects/<pid>/sessions/<sid>/send \
  -H "Authorization: Bearer mck_xxx" -H "Content-Type: application/json" \
  -d '{"text":"Spawn a teammate named coder (role: python dev). Tell it to reply '\''done'\'' to lead then stop."}'

# 4. In another shell, watch mailbox files appear/disappear
ls <workspace>/.mailboxes/
#   lead.jsonl  coder.jsonl   ← while running
# After the turn, coder.jsonl is drained by read_inbox; lead.jsonl waits for the lead

# 5. Verify cron durability + catch-up
curl ... -d '{"text":"schedule_cron at \"*/2 * * * *\" prompt \"heartbeat\". Then list_crons."}'
# Restart the backend; ask it to list_crons again
# → the same job_id should still be present (durable was persisted)

# 6. Verify wakeup is in-memory (lost on restart)
curl ... -d '{"text":"schedule_wakeup delay 10 prompt \"check\". list wakeups."}'
# Restart; list wakeups again → empty

# 7. Verify background offload: ask the agent to run npm install
curl ... -d '{"text":"Run bash: npm install (this is a slow op, expect background offload)."}'
# The main loop should return [Background task bg_xxxxxxxx started] immediately
# /bg should show running → completed

# 8. Verify /agents inbox peeks without consuming
# (in chat) /agents inbox <name>
# The same inbox should be peekable repeatedly; it's only cleared when the teammate calls read_inbox itself
```

---

## Common pitfalls / debugging

1. **"I scheduled a cron but it never fires"** — check whether an AgentLoop is actually running. cron's `tick()` is called only at the top of a main-loop iteration (`loop.py:739`). If the session is idle (nobody is talking in chat), cron does not self-wake — the current implementation has no shared ticker thread. Workarounds: a polling client driving `/loop`, or keep a resident session alive.

2. **Teammate calling spawn_teammate gets "recursion cap"** — this is the hard guard at `tools/teams.py:78`. By design only the lead can spawn one layer deep. If you truly need "team of teams", you have to change the prefix strategy in `_name_from_session` or explicitly relax this gate.

3. **plan_approval never gets a verdict** — first check whether the lead actually called `review_plan`. `/agents` reports pending counts but **does not expose request_id**; use `list_pending_requests()` or inspect `protocol._pending`. After the 600 s timeout the teammate exits on its own and marks the request `expired` (`teams/__init__.py:374`).

4. **Wakeup loss across restarts breaks `/loop`** — this is by design (`wakeup.py:1-9`). For durable self-pacing use cron with `recurring=false, durable=true`.

5. **Background result never re-enters the conversation** — `collect_notifications()` runs at the top of each main-loop iteration (`loop.py:747-755`). If the task finished but **the next main-loop iteration hasn't started** (nobody sent a message), the result sits in `BackgroundScheduler._tasks` until the next turn. Check status with `/bg`.

6. **read_inbox eats messages** — `peek_inbox` (read-only) and `read_inbox` (consume) are easy to confuse. `/agents inbox` uses peek (`commands/registry.py:650`) and is safe; the `check_inbox` tool the model calls uses read_inbox and consumes.

---

## Further reading

- Source: `mini_cc/teams/__init__.py` (teams core), `mini_cc/scheduler/{cron,wakeup}.py` (schedulers), `mini_cc/tools/{teams,cron,wakeup,bgtask,background,task}.py` (tool entries), `mini_cc/commands/registry.py` (`/agents`, `/loop`, `/bg` implementations)
- Sibling chapters: [ch 4 — Agent Loop](./04-agent-loop.md) (main loop + tool dispatch), [ch 14 — Web UI, Observability, Deployment](./14-web-ui-observability-deploy.md)
- Deployment & ops: `mini_cc/DEPLOYMENT.md`
