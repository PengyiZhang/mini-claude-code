[ < [12](12-skills-commands-lsp.md) ] [ [14](14-web-ui-observability-deploy.md) > ] · [English version](../en/13-teams-scheduler.md)

# 13 — Agent 团队与调度器

> 进阶内部原理第 13 章。本章把"单 AgentLoop 串行驱动一个会话"扩展为三种异步执行模型:**多 Agent 团队**、**秒级一次性 wakeup**、**分钟级 cron**，以及与之配套的**后台任务池**。读完你应当能解释清楚每条异步链路的线程归属、状态隔离边界与协议时序。

---

## 问题与动机

mini_cc 的核心 `AgentLoop` 是一个单线程、阻塞式的主循环(`mini_cc/core/loop.py`)。但在真实工作流里，至少有四类需求打破"一次只做一件事"的假设:

1. **并行子任务** —— 主 Agent 把"重构 + 写测试 + 改文档"派给三个 teammate 并行跑，自己只汇总结果。
2. **自我节流(self-pacing)** —— 等一个 5 分钟构建、轮询 CI、`/loop` 慢节奏迭代。需要"在 N 秒后唤醒自己"，且不能阻塞当前线程。
3. **周期触发** —— 每天早 9 点跑一次状态报告、每 10 分钟同步外部系统。需要持久化、跨重启补跑。
4. **慢命令后台化** —— `npm install`、`docker build` 这类命令不该卡住主循环，但结果要在下一轮以"task_notification"形式回到模型上下文。

这四类需求要求三个独立的执行原语:线程(teammate)、定时器(wakeup/cron)、后台 worker。它们共享同一份存储(Storage)但**状态必须隔离**——一个 teammate 的崩溃不能拖死主循环，一个 wakeup 的错配不能淹没 inbox。本章逐一拆解。

---

## 设计与原理

### 总览:三种异步链路的线程归属

```
┌──────────── HTTP 请求线程(FastAPI worker) ────────────┐
│   POST /send → AgentLoop.run(prompt) 同步迭代         │
│        │                                              │
│        ├─ _inject_cron_fired()        # 拉 cron 队列   │
│        ├─ _inject_background_notifications()  # 拉 bg   │
│        └─ should_run_background()? → BackgroundScheduler.start()
│                                  └→ 新 daemon thread    │
└───────────────────────────────────────────────────────┘
              │ spawn_teammate
              ▼
┌──────── daemon thread: teammate:<name> ───────────────┐
│   sub-AgentLoop(f"teammate-{name}").run(identity+prompt)│
│   循环: run turn → 检查 inbox → idle poll → 下一 turn  │
└───────────────────────────────────────────────────────┘

┌──── 进程内调度器(无独立线程,tick 由 loop 驱动) ────┐
│   CronScheduler.tick()   ← 每轮主循环顶部             │
│   WakeupScheduler.tick() ← 每轮主循环顶部             │
│   BackgroundScheduler    ← worker 线程,自行写回       │
└───────────────────────────────────────────────────────┘
```

关键点:cron 与 wakeup **没有自己的轮询线程**。它们的 `tick()` 在主 `AgentLoop` 每一轮迭代顶部被调用(`mini_cc/core/loop.py:733-745`)，到期的任务以一条 `[Scheduled] ...` user 消息注入当前对话。这意味着**当没有 AgentLoop 在跑时,cron 不会主动触发**——这是一个有意为之的简化(参见 `mini_cc/scheduler/cron.py:6-9` 的 TODO 注释)。如果你需要"无会话也能跑"的 cron，需要再加一个共享 ticker 线程。

### Teammate 模型:每个 teammate = 自己的 AgentLoop + session

`TeammateSpawner`(`mini_cc/teams/__init__.py:147`)是项目级的注册表。`spawn()`(`teams/__init__.py:188`)启动一个 daemon 线程，线程体 `_runner()`(`teams/__init__.py:285`)用 `loop_factory` 构造一个**全新的 sub-AgentLoop**:

```python
# mini_cc/teams/__init__.py:287
loop = self._loop_factory(f"teammate-{info.name}")
identity = (f"<identity>You are '{info.name}', a {info.role}. "
            f"Use tools to complete the requested work. "
            f"Send your final summary to 'lead' via send_message "
            f"before stopping. After calling submit_plan, end "
            f"your turn and wait for approval.</identity>")
```

session id 形如 `teammate-<name>`，这个前缀在两处起作用:

1. **tools/teams.py 的发送方识别** —— `_name_from_session()`(`tools/teams.py:18`)剥掉 `teammate-` 前缀得到 teammate 名;主 Agent 的 session 没有 `teammate-` 前缀，于是被识别为 `lead`。
2. **递归保护** —— `_spawn()`(`tools/teams.py:78`)拒绝在 teammate 上下文里再次 spawn，防止无限扇出。

每个 teammate 拥有独立的 sandbox(`projects/manager.py:_build_teammate_loop`)，所以 `set_worktree()` 把 teammate 的 sandbox 根切到某个 worktree 路径时**不会污染其他 session**(`teams/__init__.py:425`)。

### 消息总线:append-only JSONL inbox

`MessageBus`(`teams/__init__.py:37`)用 `<workspace>/.mailboxes/<name>.jsonl` 做邮箱。三条原语:

| 方法 | 行为 |
|---|---|
| `send(from, to, content, msg_type)` | 追加一行 JSON 到 `to.jsonl` |
| `read_inbox(agent)` | **drain**(读完即删) |
| `peek_inbox(agent)` | 只读，用于 idle poll 和 `/agents inbox` |

"读完即删"是有意为之:每条消息只被消费一次，避免 teammate 反复读到同一份 plan_approval_response。

### Plan-approval 协议:回合间的同步闸

```
teammate turn N: 调用 submit_plan(plan)
     ↓
ProtocolTracker.register(ProtocolState(type="plan_approval"))
     ↓ bus.send → lead.jsonl: plan_approval_request
teammate _wait_for_plan_verdict(): 阻塞轮询 inbox
     ↓                                  ↑
     │ (10 分钟超时, plan_approval_timeout)│
     ↓                                  │
lead turn: review_plan(request_id, approve=True)
     ↓ bus.send → teammate.jsonl: plan_approval_response
     ↓
teammate 收到 verdict → next_input = "[Plan approved]" → 进入 turn N+1
```

注意 plan-approval 是**回合间闸**，不是回合内闸(`teams/__init__.py:11-14`):模型在 `submit_plan` 后被指示结束当前回合，spawner 在下一回合开始前阻塞。这避免了在生成器中途打断 LLM 流的复杂性。`plan_approval_timeout` 默认 600 秒(`teams/__init__.py:177`)——够人在工作时间审，又不会让死掉的 lead 把 teammate 锁死。

### Wakeup:秒级、内存内、一次性

`WakeupScheduler`(`scheduler/wakeup.py:29`)是纯内存的。`schedule()`(`wakeup.py:40`)用 `time.monotonic()` 加 delay 算 `fire_at`，`tick()`(`wakeup.py:67`)返回并删除所有到期的 wakeup。

设计约束(见 `tools/wakeup.py:17-20`、`wakeup.py:1-9`):

- **延迟上限 3600 秒**(1 小时)。更长用 cron。
- **不跨重启**。进程死了 pending wakeup 全丢——这是有意的，durability 由 cron 负责。
- **每个 AgentLoop 持有自己的实例**(注释 `wakeup.py:30-33`)，非线程安全。

工具入口 `schedule_wakeup`(`tools/wakeup.py:95`)、`list_wakeups`、`cancel_wakeup` 三个。

### Cron:分钟级、可持久化、跨重启补跑

`CronScheduler`(`scheduler/cron.py:102`)支持标准 5 字段 cron(`min hour dom month dow`，POSIX 语义，DOW 0=Sun)。两个关键设计:

1. **持久化** —— `durable=True` 的 job 通过 `storage.save_cron()` 落盘(`cron.py:133-135`)。
2. **B12 catch-up 窗口** —— 重启后 `_catch_up_missed_locked()`(`cron.py:196`)向前扫描最多 24 小时(`MAX_CATCHUP_WINDOW_SECONDS`，`cron.py:108`)，**每个 job 至多补一次**(取窗口内最近的匹配分钟)，避免长时间宕机后涌入大量补跑。

POSIX 的 DOM/DOW "或"语义在 `cron_matches()`(`cron.py:48-54`)实现:两者都非 `*` 时任一匹配即触发。

### 后台任务:`should_run_background` 闸 + 协作式取消

`should_run_background()`(`tools/background.py:38`)是唯一的入队判据:

```python
# mini_cc/tools/background.py:38-42
def should_run_background(tool_name, tool_input):
    if tool_name != "bash":
        return False
    return bool((tool_input or {}).get("run_in_background")) \
        or is_slow_operation(tool_name, tool_input)
```

`is_slow_operation` 用关键字列表(`install|build|test|deploy|compile|...`，`background.py:26-28`)粗略匹配 bash 命令。命中后 `BackgroundScheduler.start()`(`background.py:70`)起一个 daemon 线程跑 handler，主循环立刻返回 `[Background task bg_xxxxxxxx started]`。完成后下一轮 `collect_notifications()`(`background.py:199`)把它包成 `<task_notification>` XML 注入对话。

`stop()`(`background.py:159`)是**协作式取消**:设置 `cancel_event`，sandbox.execute 每 100ms 轮询一次，发现就 SIGTERM 子进程。Python 线程本身不可硬杀。

---

## 操作与配置

### 团队相关工具

| 工具 | 入参 | 谁能调用 | 来源 |
|---|---|---|---|
| `spawn_teammate` | `name, prompt, role?` | 仅 lead(递归闸) | `tools/teams.py:166` |
| `send_message` | `to, content, msg_type?` | lead + teammate | `tools/teams.py:126` |
| `check_inbox` | (无) | lead + teammate | `tools/teams.py:141` |
| `list_teammates` | (无) | lead | `tools/teams.py:148` |
| `request_shutdown` | `name` | lead | `tools/teams.py:155` |
| `submit_plan` | `plan` | 仅 teammate | `tools/teams.py:181` |
| `review_plan` | `request_id, approve, feedback?` | lead | `tools/teams.py:207` |

### Slash 命令

| 命令 | 作用 | 来源 |
|---|---|---|
| `/agents` | 列出所有 teammate(alive + 已停止)，含角色、年龄、inbox 计数 | `commands/registry.py:560` |
| `/agents stop <name>` | 给 teammate 发 shutdown_request | `commands/registry.py:579` |
| `/agents inbox <name>` | **peek**(不消费)某 teammate 的 inbox | `commands/registry.py:589` |
| `/loop` | 列出 cron + wakeup 任务 | `commands/registry.py:716` |
| `/loop cancel <id>` | 取消一个 cron 或 wakeup | `commands/registry.py:739` |
| `/bg` | 列出后台任务 | `commands/registry.py:1051` |
| `/bg stop <bg_id>` | 取消后台任务 | `commands/registry.py:1063` |

### 调度器配置(构造参数)

| 参数 | 默认 | 说明 |
|---|---|---|
| `idle_poll_interval` | 5.0s | teammate idle 轮询 inbox 的间隔 |
| `idle_timeout` | 60.0s | teammate idle 多久后退出 |
| `plan_approval_timeout` | 600.0s | teammate 等 plan 审批的超时 |
| `MAX_DELAY_SECONDS`(wakeup) | 3600 | wakeup 工具硬上限 |
| `MAX_CATCHUP_WINDOW_SECONDS`(cron) | 86400 | cron 重启补跑窗口 |

> 这些目前**不可通过 env 配置**——它们是 `TeammateSpawner.__init__`(`teams/__init__.py:160-177`)和 `tools/wakeup.py:20` 的 Python 常量。要改需要改源码或在 `projects/manager.py:317` 构造 spawner 时传入。

---

## 验证步骤

以下假设后端跑在 `:8002`(本仓库 e2e 默认端口)，tenant key 已通过 `python -m mini_cc.server keygen demo` 生成。

```bash
# 1. 启动后端
python -m mini_cc.server  # 监听 :8000；e2e 用 :8002

# 2. 准备:创建 tenant key + project + session(略，参考第 7 章 HTTP server)

# 3. 验证 spawn + 收敛:让 lead spawn 一个 teammate，让它 send_message 回来
curl -N :8002/tenants/demo/projects/<pid>/sessions/<sid>/send \
  -H "Authorization: Bearer mck_xxx" -H "Content-Type: application/json" \
  -d '{"text":"Spawn a teammate named coder (role: python dev). Tell it to reply '\''done'\'' to lead then stop."}'

# 4. 在另一终端观察 mailbox 文件出现与消失
ls <workspace>/.mailboxes/
#   lead.jsonl  coder.jsonl   ← 运行中
# 回合结束后 coder.jsonl 被 read_inbox 删除，lead.jsonl 留着等 lead 读

# 5. 验证 cron 持久化 + 补跑
curl ... -d '{"text":"schedule_cron at \"*/2 * * * *\" prompt \"heartbeat\". Then list_crons."}'
# 重启后端，看日志或 GET /tenants/.../sessions/.../send 让它 list_crons
# → 同一个 job_id 应当还在(durable 落盘了)

# 6. 验证 wakeup 是内存态(重启即丢)
curl ... -d '{"text":"schedule_wakeup delay 10 prompt \"check\". list wakeups."}'
# 重启后端，再 list wakeups → 空

# 7. 验证后台任务:让 agent 跑 npm install
curl ... -d '{"text":"Run bash: npm install (this is a slow op, expect background offload)."}'
# 主循环应立即返回 [Background task bg_xxxxxxxx started]
# /bg 应能看到 running → completed

# 8. 验证 /agents inbox peek 不消费
# (在 chat 里) /agents inbox <name>
# 同一 inbox 应当能被 /agents inbox 反复读，直到 teammate 自己 read_inbox 才清空
```

---

## 常见坑与调试

1. **"我 schedule 了 cron 但到点没触发"** —— 检查有没有 AgentLoop 在跑。cron 的 `tick()` 只在主循环迭代顶部调用(`loop.py:739`)。如果会话空闲(没有人在 chat 里说话)，cron 不会主动唤醒——当前实现没有共享 ticker 线程。临时解法:用 `/loop` 命令的轮询客户端，或保持一个常驻会话。

2. **teammate 调 spawn_teammate 报"recursion cap"** —— 这是 `tools/teams.py:78` 的硬保护。设计上只允许 lead 一层派发。如果你真要"team of teams"，得改 `_name_from_session` 的前缀策略或显式放宽这个闸。

3. **plan_approval 永远等不到** —— 先看 lead 是不是真的调了 `review_plan`。`/agents` 会列出 pending 计数，但**不会告诉你 request_id**;要用 list_pending_requests() 或检查 `protocol._pending`。超时 600s 后 teammate 会自动退出并标记 request 为 `expired`(`teams/__init__.py:374`)。

4. **wakeup 跨重启丢失导致 `/loop` 断流** —— 这是设计内行为(`wakeup.py:1-9`)。需要持久化的自我节流应改用 cron(recurring=false, durable=true)。

5. **后台任务结果没回到对话** —— `collect_notifications()` 在主循环每轮顶部调用(`loop.py:747-755`)。如果任务跑完但**下一轮主循环还没开始**(没人发消息)，结果会停在 `BackgroundScheduler._tasks` 里直到下一轮。用 `/bg` 查状态即可。

6. **read_inbox 把消息吃了** —— `peek_inbox`(只读)与 `read_inbox`(消费)容易混。`/agents inbox` 用的是 peek(`commands/registry.py:650`)，安全;模型直接调 `check_inbox` 工具用的是 read_inbox，会消费。

---

## 延伸阅读

- 源码:`mini_cc/teams/__init__.py`(团队核心)、`mini_cc/scheduler/{cron,wakeup}.py`(调度器)、`mini_cc/tools/{teams,cron,wakeup,bgtask,background,task}.py`(工具入口)、`mini_cc/commands/registry.py`(`/agents`、`/loop`、`/bg` 实现)
- 同系列:[第 4 章 Agent Loop](./04-agent-loop.md)(主循环与工具派发)、[第 14 章 Web UI/可观测性/部署](./14-web-ui-observability-deploy.md)
- 部署与运维:`mini_cc/DEPLOYMENT.md`、`docs/zh/2026-06-28-deploy-runbook.zh.md`
