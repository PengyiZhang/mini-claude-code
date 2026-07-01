[ < [12](12-skills-commands-lsp.md) ] [ [13b](13b-scheduler.md) > ] · [English version](../en/13-teams-scheduler.md)

# 13a — Agent 团队（Teammates）

> 进阶内部原理第 13a 章。本章只讲"多 Agent 团队"这一异步执行原语：每个 teammate 跑在自己专属的 sub-AgentLoop + daemon 线程里，通过 JSONL 邮箱通信，必要时回合间排队等 lead 审 plan。读完你应当能说清楚：teammate 何时被唤醒、何时退出、@mention 怎么走 SSE、重启后会丢什么。调度器（wakeup / cron / 后台任务）在[第 13b 章](./13b-scheduler.md)单独讲。

---

## 问题与动机

mini_cc 的核心 `AgentLoop` 是一个单线程、阻塞式的主循环（`mini_cc/core/loop.py`）。主 Agent 一次只能干一件事。但在真实工作流里至少有三类需求打破"一次只做一件事"的假设：

1. **并行子任务** —— 主 Agent 把"重构 + 写测试 + 改文档"派给三个 teammate 并行跑，自己只汇总结果。
2. **角色分工** —— researcher 收集资料 → coder 落地 → reviewer 审，每个角色独立会话，避免上下文互相污染。
3. **隔离的长链工作** —— 某个子任务需要几十轮工具调用，主线只想拿最终摘要，不想把中间过程塞进自己的 session。

teammates 子系统就是为这三类需求设计的执行原语。**它明确不解决**下面这些问题：

- **跨进程协作** —— teammate 是同进程的 daemon 线程（`teams/__init__.py:211`），不能跨 worker / 跨机器。
- **跨租户通信** —— `TeammateSpawner` 是 project 级注册表（`projects/manager.py` 装配时每个 project 一个），mailbox 文件就在该 project 的 workspace 下。
- **强一致协商** —— teammate 与 lead 之间是消息总线 + 单条 plan-approval 协议，不是分布式共识。

如果你需要的是 wakeup / cron / 后台任务，请看 [13b](./13b-scheduler.md)；本章下面只讲 teammates。

---

## 心智模型：一个 teammate = 一个 sub-AgentLoop + 独立 session + 独立 sandbox

`TeammateSpawner`（`mini_cc/teams/__init__.py:161`）是 project 级的注册表。`spawn()`（`teams/__init__.py:202`）启动一个 daemon 线程，线程体 `_runner()`（`teams/__init__.py:373`）用 `loop_factory` 构造一个**全新的 sub-AgentLoop**：

```python
# mini_cc/teams/__init__.py:375
loop = self._loop_factory(f"teammate-{info.name}")
identity = (f"<identity>You are '{info.name}', a {info.role}. "
            f"Use tools to complete the requested work. "
            f"Send your final summary to 'lead' via send_message "
            f"before stopping. After calling submit_plan, end "
            f"your turn and wait for approval.</identity>")
```

session_id 形如 `teammate-<name>`，这个前缀在两处起作用：

1. **`tools/teams.py:_name_from_session`**（`tools/teams.py:18-22`）剥掉 `teammate-` 前缀得到 teammate 名；主 Agent 的 session 没有 `teammate-` 前缀，于是被识别为 `lead`。
2. **`_spawn` 的递归闸**（`tools/teams.py:96-98`）拒绝在 teammate 上下文里再次 spawn，防止无限扇出。

每个 teammate 还拥有独立的 sandbox（`projects/manager.py:_build_teammate_loop`），所以 `set_worktree()` 把 teammate 的 sandbox 根切到某个 worktree 路径时**不会污染其他 session**（`teams/__init__.py:531-532`）。

```
┌─────────── 主线程（HTTP worker） ────────────┐
│   AgentLoop.run(prompt)                      │
│     ├─ tool: spawn_teammate(alice, ...)      │
│     ├─ tool: send_message(to=alice, ...)     │
│     │     ↘ bind_event_sink(alice, fresh_sink)   ← 每次重绑
│     └─ tool: review_plan(req_id, approve=…)   │
└──────────────────────────────────────────────┘
             │ mailbox 文件（JSONL）
             ▼
┌────── daemon thread: teammate:alice ─────────┐
│   sub-AgentLoop("teammate-alice").run(...)   │
│   循环:                                       │
│     1. run 一整轮（含工具调用）                │
│     2. peek inbox → shutdown? → 退出          │
│     3. plan_approval pending? → 阻塞等审      │
│     4. _idle_poll → 收消息 / 自动认领 task    │
│     5. persistent=True 时回到第 1 步           │
└──────────────────────────────────────────────┘
```

---

## 生命周期

### 状态机

```
absent ──spawn()──▶ alive ⇄ idle_polling
                      │
                      ├── 新 inbox 消息 / 自动认领 task ──▶ run 一轮
                      ├── submit_plan ──▶ 阻塞等 review_plan
                      ├── 收到 shutdown_request ──▶ exiting
                      └── idle_timeout（仅 persistent=False）──▶ exiting

alive ──stop()──▶ exiting ──▶ stopped（registry 保留最近 3 个）
                                  │
                                  ├── /agents edit（改 role / prompt）
                                  ├── /agents delete（彻底移除）
                                  └── 同名 spawn ──▶ 复活回 alive
```

### 关键时间常量

来自 `TeammateSpawner.__init__` 默认参数（`teams/__init__.py:174-180`）：

| 常量 | 默认 | 含义 |
|---|---|---|
| `idle_poll_interval` | **5.0 s** | teammate idle 时轮询 inbox 的间隔 |
| `idle_timeout` | **60.0 s** | teammate 空闲多久后**才考虑**退出（仅 `persistent=False` 生效） |
| `plan_approval_timeout` | **600.0 s** | teammate 提交 plan 后等 lead 审的最长时间，超时自动退出 |

> 这些目前**不可通过 env 配置**——它们是 Python 构造参数，要改需要改源码或在 `projects/manager.py` 装配 spawner 时显式传入。

### `persistent` 标志的两种语义

默认 `persistent=True`（`teams/__init__.py:148`）—— **常驻**：

```python
# mini_cc/teams/__init__.py:440-445
if result == "timeout":
    # debug.7.md Task 1: 手工 spawn 的 teammate 常驻 —
    # persistent=True 时继续等待，仅 persistent=False 才退出。
    if info.persistent:
        continue
    break
```

- `persistent=True`：idle 超时也不退，继续在 `_idle_poll` 里等消息或新 task。这是**手工 spawn 的默认行为**，符合"我开了 alice 让它一直在那儿待命"的直觉。
- `persistent=False`：idle 超过 60 秒就退。这是**遗留行为**，现在主要出现在测试和某些工具自动派生的场景。

`spawn_teammate` 工具（`tools/teams.py:108`）和 `/agents spawn` 命令目前都使用默认值 `True`。如果你的测试想看到"自动退出"行为，需要直接调 `spawner.spawn(..., persistent=False)`。

### 停止后保留在 registry 里

`_KEEP_STOPPED = 3`（`teams/__init__.py:240`）：每个 project 的 spawner 至多保留 **3 个最近停止的 teammate** 条目，超过就按 `stopped_at` 时间 FIFO 剔除（`_prune_stopped_locked`，`teams/__init__.py:242-250`）。保留是为了让 `/agents` 能列出"最近停止"以便 `/agents edit` 或 `/agents delete` 操作；活着的条目不计入这个上限。

---

## 消息总线：append-only JSONL 邮箱

`MessageBus`（`teams/__init__.py:37`）用 `<workspace>/.mailboxes/<name>.jsonl` 做邮箱。三条原语：

| 方法 | 行为 | 出处 |
|---|---|---|
| `send(from, to, content, msg_type)` | 追加一行 JSON 到 `to.jsonl` | `teams/__init__.py:54-63` |
| `read_inbox(agent)` | **drain**（读完即删整个文件） | `teams/__init__.py:65-73` |
| `peek_inbox(agent)` | 只读，用于 idle poll 和 `/agents inbox` | `teams/__init__.py:75-82` |

"drain"是有意为之：每条消息只被消费一次，避免 teammate 反复读到同一份 plan verdict。"peek"则是给 UI / 测试用的，不消费。

一条消息的字段（`teams/__init__.py:57-59`）：

```json
{"from": "lead", "to": "alice", "content": "...", "type": "message",
 "ts": 1782780000.0, "metadata": {}}
```

`type` 是协议路由用的：`message` / `shutdown_request` / `shutdown_response` / `plan_approval_request` / `plan_approval_response` / `result`。

---

## Plan-approval 协议：回合间的同步闸

### 时序

```
teammate turn N: 调用 submit_plan(plan)
     ↓
ProtocolTracker.register(ProtocolState(type="plan_approval", status="pending"))
     ↓ bus.send → lead.jsonl: plan_approval_request
teammate _wait_for_plan_verdict(): 阻塞轮询自己的 inbox
     ↓                                       ↑
     │ (超时 plan_approval_timeout=600s)     │
     ↓                                       │
lead turn: review_plan(request_id, approve=True)
     ↓ protocol.update_status(approved)
     ↓ bus.send → teammate.jsonl: plan_approval_response
     ↓
teammate 收到 verdict → next_input = "[Plan approved]" → 进入 turn N+1
```

### 关键设计：是**回合间闸**，不是回合内闸

源码顶部的设计注释（`teams/__init__.py:11-14`）说得很清楚：plan-approval 在两个回合之间排队等，而不是在生成器中途打断 LLM 流。模型在 `submit_plan` 后被指示结束当前回合（"End your turn and wait"），spawner 在**下一回合开始前**阻塞。这避免了"在 LLM 流式生成中拦截"的复杂性。

### 同时跟踪的 `shutdown` 协议

`ProtocolTracker` 同时管 `plan_approval` 和 `shutdown` 两类（`teams/__init__.py:91`）。后者由 lead 的 `request_shutdown`（`teams/__init__.py:252-263`）发起，teammate 在 `_idle_poll` 或 `_wait_for_plan_verdict` 里检测到后调用 `_send_shutdown_response` 并退出。

### 超时与失败

- **600 秒等不到 review** —— teammate 自动退出，protocol 标记为 `expired`（`teams/__init__.py:474-484`），lead 之后才发 review 也不会有效果。
- **lead 想"我已经审了但 teammate 不在"** —— 看 `list_pending_requests()` 里 status 是不是 `expired`。

---

## Event sink 重绑定：让 @mention 走 live SSE

**这是最容易理解错的地方**，特别拎出来一节讲。

### 问题：spawn 时的 sink 会失效

`spawn_teammate` 工具会把当前的 `ctx.on_subagent_event` 作为 sink 传给 spawner（`tools/teams.py:107-109`）。但 `ctx.on_subagent_event` 是 `AgentLoop._execute_tool_calls` 在**每次工具调用时**临时建立的（注释见 `tools/teams.py:99-106`）。spawn 工具一旦返回，那个 sink 指向的 SSE 流就已经关闭了。

后续回合里 lead 想 @mention teammate，teammate 醒过来跑工具调用产生 events——**这些 events 会发到已经死掉的 sink**，UI 看不到。

### 解法：每次 lead 发消息都重新绑

`tools/teams.py:_send` 在 `from_=="lead"` 时**强制重绑** sink（`tools/teams.py:40-41`）：

```python
# mini_cc/tools/teams.py:34-42
from_ = _name_from_session(ctx) or "lead"
# debug.7.md Task 1c: lead-side @mention must re-bind the teammate's
# event sink so events from the wake-up turn flow into the current
# SSE stream (the spawn-time sink went stale when spawn_teammate
# returned). Teammate-to-teammate sends don't need this — their
# events already route through their own runner's sink.
if from_ == "lead" and hasattr(spawner, "bind_event_sink"):
    spawner.bind_event_sink(to, ctx.on_subagent_event)
spawner.bus.send(from_, to, content, msg_type)
```

`bind_event_sink`（`teams/__init__.py:220-235`）只更新 `info.event_sink` 字段。`_runner` 里的 `_forward` 函数在**每次发射 event 时重新读取**这个字段（`teams/__init__.py:382-393`），保证拿到的是最新绑定：

```python
# mini_cc/teams/__init__.py:382-392
def _forward(ev: dict) -> None:
    """Live event forwarding — re-read info.event_sink each call
    so bind_event_sink takes effect on the next event."""
    with self._lock:
        sink = info.event_sink
    if sink is not None:
        try:
            sink(ev)
        except Exception:
            pass  # A broken sink must never tear down the teammate.
```

### 实操含义

- **spawn 完直接 @mention 是不会刷到 UI 的**——必须 lead 先发一次消息（哪怕 `send_message` 也行），sink 才会重绑到当前 SSE 流。
- **每次 `send_message` 都会重绑**——不重绑的话，lead 下一轮 SSE 流又变了，老 sink 又失效。
- **teammate 之间互发不需要重绑**——`if from_ == "lead"` 这个条件放过 teammate。
- **sink 抛异常不会拖死 teammate**——`_forward` 用 `try/except` 全吞，注释明说"A broken sink must never tear down the teammate"（`teams/__init__.py:391`）。

---

## 工具一览

全部 8 个工具的入参、谁能调、一句话行为：

| 工具 | 入参 | 谁能调 | 行为 | 出处 |
|---|---|---|---|---|
| `spawn_teammate` | `name, role, prompt` | 仅 lead（递归闸） | 派生新 teammate；lead 的 sink 传给 teammate | `tools/teams.py:85-112, 184-197` |
| `send_message` | `to, content, msg_type?` | 双向 | 写入收件人 mailbox；lead 调用时重绑 sink；目标已停止时返回警告 | `tools/teams.py:25-54, 144-157` |
| `check_inbox` | — | 双向 | **drain** 自己的 inbox（读完即删） | `tools/teams.py:57-65, 159-164` |
| `list_teammates` | — | 双向 | 列出所有 alive teammates，`name: role` 一行一个 | `tools/teams.py:68-75, 166-171` |
| `request_shutdown` | `name` | 仅 lead | 发 `shutdown_request` 协议消息 | `tools/teams.py:78-82, 173-182` |
| `submit_plan` | `plan` | 仅 teammate | 注册 pending plan，写入 lead mailbox，下一回合阻塞 | `tools/teams.py:115-125, 199-209` |
| `request_plan` | `teammate, task` | 仅 lead | 让某 teammate 起草一份 plan | `tools/teams.py:128-132, 211-223` |
| `review_plan` | `request_id, approve, feedback?` | 仅 lead | 批 / 拒一个 pending plan | `tools/teams.py:135-141, 225-238` |

> **drain vs peek 的坑：** `check_inbox` 工具用的是 `read_inbox`（**消费**，读完删文件）；`/agents inbox` 命令用的是 `peek_inbox`（**只读**）。两者不可互换。

---

## Slash 命令：`/agents`

无参时返回一个 list 卡片（`commands/registry.py:1127-1185`），alive + 最近停止都在里面。每行带：

- **title**：teammate 名
- **subtitle**：`role · age {Nm/Nh/Nd}`
- **badges**：`alive`（ok）/ `stopped`（default）、`📨 N`（有未读时）、`wt:{name}`（绑了 worktree 时）
- **expandable_command**：`/agents inbox {name}` —— 点行内联展开看 inbox
- **菜单**：alive → `stop`；stopped → `edit` + `delete`（`commands/registry.py:1204-1222`）

子命令（`commands/registry.py:1001-1126`）：

| 命令 | 用法 | 行为 |
|---|---|---|
| `/agents` | 无参 | 列卡片 |
| `/agents spawn` | `<name> <role> --prompt <text>` | 派生新 teammate；`--prompt` 必填 |
| `/agents stop` | `<name>` | 发 `shutdown_request`，不等线程退出 |
| `/agents inbox` | `<name>` | **peek**（不消费），最多 10 条，每条截 80 字 |
| `/agents delete` | `<name>` | 仅停止态可用；强制先 stop |
| `/agents edit` | `<name> [--role <r>] [--prompt <p>]` | 仅停止态可用；至少一个 flag |

`--prompt <text>` 解析在 `_parse_spawn_args`（`commands/registry.py:1257-1286`），文本可以含空格，支持外层引号剥离。`--role` 和 `--prompt` 顺序无关（`_parse_edit_args`，`commands/registry.py:1225-1254`）。

> **设计约束：** `delete` / `edit` 拒绝在 alive 状态下操作。原因：运行中的 loop 已经捕获了原始 role/prompt；中途改字段对当前 turn 无效，反而让 registry 与现实不一致。要改先 stop。

---

## 与 Workflow V2 的集成：checkpoint approvers

### 触发场景

Workflow V2（见[第 10 章](./10-workflow-v2.md)）跑到 `checkpoint` 步时，UI 要弹"选 approver"下拉。`step.config.approvers` 是个自由字符串列表，UI 不知道该填什么——这就是 `list_candidate_approvers` 要解决的。

### 返回结构

`mini_cc/workflow/approvers.py:22-65` 的 `list_candidate_approvers(project)` 返回**有序列表**：

1. **alive teammates**（按名字排序）
2. **stopped teammates**（也列出，flag `alive=False`，让用户可以选已停的角色名）
3. **合成 `lead` 条目**（除非已有 teammate 名叫 `lead`，避免重复）
4. **合成 `human` 占位**（`name=""`，UI 弹自由输入框）

每项结构：

```python
{"name": "alice", "type": "teammate", "alive": True, "role": "reviewer"}
{"name": "lead",  "type": "lead",     "alive": True, "role": "main agent"}
{"name": "",      "type": "human",    "alive": True, "role": "any human (type a name)"}
```

### HTTP 端点

```python
# mini_cc/server/routes/workflow_v2.py:302-314
@runs_router.get("/{run_id}/steps/{step_id}/approvers")
def list_approvers(...):
    # 走标准 tenant 边界检查
    svc = _service_for(pm, pid, tid)
    project = pm.get(pid)
    from ...workflow.approvers import list_candidate_approvers
    return list_candidate_approvers(project)
```

### 与 `step.config.approvers` 的关系

- **`config.approvers` 是约束**（"只能这些人批"）：服务端 `resolve_gate` 会校验提交的 `approver` 字段。
- **`list_candidate_approvers` 是建议**（"UI 给个下拉"）：纯描述性，不影响审批逻辑本身。

---

## 持久化边界：重启后会丢什么

| 项 | 是否跨重启 | 说明 |
|---|---|---|
| Teammate 线程 | ❌ | 线程对象必然死 |
| `TeammateInfo` 注册表 | ❌ | 进程内 dict（`teams/__init__.py:193`） |
| `event_sink` callback | ❌ | 绑在已死的 HTTP 请求 / SSE 流上 |
| Mailbox JSONL 文件 | ✅ | 落盘在 `<workspace>/.mailboxes/`，重启后仍可读 |
| `ProtocolTracker` pending | ❌ | 内存 dict（`teams/__init__.py:109`）；pending 请求会丢，等 verdict 的 teammate 会卡到 timeout |
| Tasks（自动认领的） | ✅ | 走 Storage 落盘，重启后还能被认领 |

**实操影响：**

- 重启后所有 teammate 都没了，需要重新 spawn。
- 重启前 mailbox 里没读的消息还在；但**当时 pending 的 plan_approval 等不到回应了**——新 spawn 的 teammate 不知道旧 request_id。
- 如果 lead 重启后想"恢复"某个 teammate，重新 `/agents spawn <同名> <同role> --prompt ...` 即可；mailbox 里堆积的消息会被它读走。

---

## 常见坑与调试

1. **`persistent=True` 默认不退** —— 想让 teammate 自动退必须显式传 `persistent=False`。`/agents` 列表里年龄几小时的 teammate 不一定是僵尸，可能就是常驻。

2. **sink 失效导致 @mention 不刷到 UI** —— 必须 lead 主动发一次消息（或调 `send_message` 工具）才会重绑。spawn 后干等是等不到 events 进 UI 的。详见上面"Event sink 重绑定"一节。

3. **向 stopped teammate 发消息不会丢，但也不会复活** —— `_send`（`tools/teams.py:48-53`）会写进 mailbox 并返回警告：
   ```
   Sent to alice (queued — alice is stopped; restart via /agents spawn to process)
   ```
   要让消息被处理，必须重新 `/agents spawn alice ...` 把它拉起来。

4. **plan_approval 600 秒超时** —— teammate 默认会自动退出并标 protocol 为 `expired`。想拖久要改 `TeammateSpawner.__init__` 的 `plan_approval_timeout` 参数（源码常量，无 env）。

5. **`/agents inbox` 是 peek，`check_inbox` 工具是 drain** —— 别混淆。前者只读，可以反复调；后者每次都把整个 mailbox 文件删掉。

6. **`/agents delete` / `edit` 必须先 stop** —— alive 时强制报错。设计原因见 `teams/__init__.py:330-345` 的 docstring。

7. **registry 只保留最近 3 个 stopped**（`_KEEP_STOPPED = 3`）—— 老 stopped 会被自动 FIFO 清理；想长期保留某个角色名，要么 keep alive，要么接受重新 spawn。

8. **`teammate-` 前缀是 reserved** —— 别把主 session 命名成 `teammate-xxx`，会被 `_name_from_session`（`tools/teams.py:18-22`）误判成 teammate。

---

## 验证步骤

以下假设后端跑在 `:8002`，tenant key 已通过 `python -m mini_cc.server keygen demo` 生成。

```bash
# 1. spawn 一个常驻 teammate
curl -N :8002/tenants/demo/projects/<pid>/sessions/<sid>/send \
  -H "Authorization: Bearer mck_xxx" -H "Content-Type: application/json" \
  -d '{"text":"Spawn a teammate named alice (role: researcher). Tell her to wait for messages."}'

# 2. 看 mailbox 文件出现
ls <workspace>/.mailboxes/
#   lead.jsonl   alice.jsonl

# 3. /agents 列卡片（在 chat 里）—— alice 应当在 alive 行
#    /agents
#    /agents inbox alice

# 4. 验证 plan-approval：让 alice submit_plan，lead review
curl ... -d '{"text":"Ask alice to submit_plan for: refactor X. Then approve."}'

# 5. 验证 sink 重绑：spawn 后直接 @mention 看 UI 是否刷
#    （不刷 → 这是预期；让 lead 发一句话再 @mention → 应当刷）

# 6. 验证持久化边界：
#    /agents stop alice   # 让她进 stopped
#    重启后端
#    /agents              # alice 应当消失（registry 是内存）
#    ls <workspace>/.mailboxes/   # alice.jsonl 可能还在（未读消息）

# 7. /agents delete alice  # 必须先 stop
#    /agents edit alice --role senior-researcher   # 同上
```

---

## 延伸阅读

- 源码：
  - `mini_cc/teams/__init__.py` —— `MessageBus` / `ProtocolTracker` / `TeammateSpawner` / `TeammateInfo` / `_runner`
  - `mini_cc/tools/teams.py` —— 8 个工具 + 发送方识别 + sink 重绑逻辑
  - `mini_cc/commands/registry.py` —— `_cmd_agents`（`registry.py:985`）和 `/agents` 子命令解析
  - `mini_cc/workflow/approvers.py` —— checkpoint approver 候选生成
  - `mini_cc/server/routes/workflow_v2.py:302` —— approvers HTTP 端点
- 同系列：
  - [第 4 章 Agent Loop](./04-agent-loop.md) —— 主循环与工具派发
  - [第 13b 章 调度器（wakeup / cron / 后台任务）](./13b-scheduler.md) —— 与 teammates 共享"异步原语"主题但完全独立的子系统
  - [第 10 章 Workflow V2](./10-workflow-v2.md) —— checkpoint 步如何调用 `list_candidate_approvers`
- 部署与运维：`mini_cc/DEPLOYMENT.md`、`docs/zh/2026-06-28-deploy-runbook.zh.md`
