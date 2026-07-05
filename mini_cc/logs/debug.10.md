
目前主session会话显示还存在如下问题:


实时运行时的UI 主session目前展示就是如下，自动刷新到alice的消息后就停了：

```
让它们逐个进行自我介绍吧
U
L

▶
🔧 send_message
alice
✓ done

▶
🔧 schedule_wakeup
5
✓ done
我会让它们各自进行自我介绍。既然它们是按顺序“逐个”介绍，我会按序发送指令——先从 alice 开始。等待

A
@alice
Hi! I'm alice, a teammate on this team. My role is to pick up assigned tasks, collaborate with my peers, and ship work back to you — reporting progress by milestone and a final result when my mission is complete. Ready to roll whenever you've got work for me.

```

但是重新手动刷新页面后，就显示完整如下：

```
让它们逐个进行自我介绍吧
U
L

▶
🔧 send_message
alice
✓ done

▶
🔧 schedule_wakeup
5
✓ done
我会让它们各自进行自我介绍。既然它们是按顺序“逐个”介绍，我会按序发送指令——先从 alice 开始。等待唤醒触发，这样我就能查看 Alice 的回复并触发 Bob。

[Teammate messages]
- from: alice, type: message, cc: False
  content: Hi! I'm alice, a teammate on this team. My role is to pick up assigned tasks, collaborate with my peers, and ship work back to you — reporting progress by milestone and a final result when my mission is complete. Ready to roll whenever you've got work for me.
- [scheduled wakeup] Check alice's reply, then ask bob to introduce itself.
U
L

▶
🔧 send_message
Please introduce yourself. Send a short self-introduction (w…
✓ done

▶
🔧 schedule_wakeup
5
✓ done
Alice 的介绍已经收到。现在正在触发 Bob。

<teammate_messages>[{"from": "bob", "to": "lead", "content": "Hi, I'm bob — a teammate on this team. I'm a generalist coding agent ready to take on whatever work you delegate: writing code, investigating issues, running builds/tests, coordinating with alice and carl, and reporting results back to you. Standing by for tasks.", "type": "message", "ts": 1783255692.3874514, "metadata": {}}]</teammate_messages>
U
L

▶
🔧 send_message
Please introduce yourself. Send a short self-introduction (w…
✓ done
Bob's intro is in. Now triggering carl.正在等待 Carl 的回复，以完成自我介绍。

- [scheduled wakeup] Check bob's reply, then ask carl to introduce itself.
U
L
That wakeup is stale (bob already replied and carl has been triggered). Waiting for carl's intro.

[Teammate messages]
- from: carl, type: message, cc: False
  content: Hi, I'm carl — a teammate on this team. My role is to pick up assigned tasks, execute them with the available tools, coordinate with my peers (alice and bob), and report results back to you. Ready for my first assignment whenever you are.
U
L
All three teammates have introduced themselves. Here's the round-up:

Teammate	Self-introduction
alice	Picks up assigned tasks, collaborates with peers, ships work back reporting progress by milestone.
bob	Generalist coding agent — writes code, investigates issues, runs builds/tests, coordinates with alice and carl.
carl	Picks up assigned tasks, executes them with the available tools, coordinates with peers, reports results.
The team is assembled and all members are standing by. Want me to assign them some work, or run a kickoff task together?
```

而实时刷新时，右边栏的TIMELINE则正常能够刷新所有内容


现在希望的是Lead Session在唤醒时也能在主session对话UI中唤醒，持续刷新消息


---

## 修复 (2026-07-05)

### 根因

`/send` 的 SSE 流是**请求作用域**的：用户提交后开启，那一轮 AgentLoop
迭代结束后就关闭了。但 LeadSession 在 LeadWatcher 守护线程里被唤醒
（`schedule_wakeup` 触发，或队友回复触发 nudge），这些守护回合通过
`watcher_event_sink` 把事件写进了 `events.jsonl`，**没有任何 /send 流
打开**去把它们推送给主对话 UI。结果：UI 永远停在用户那一轮的最后一条
事件上。

右边栏 Timeline 正常，是因为它独立轮询 `/team/activity`；手动刷新页面
正常，是因为 `/messages` 从磁盘重新水合。

### 方案 — Option α: 双流 + 按 seq 去重

- **`/send` 不变**：仍然是用户回合的实时流（保留"打字感"）。
- **新增 `GET /sessions/{sid}/events` 长连 SSE**：连接时若带
  `Last-Event-Id` 则回放错过的；不带则跳过历史（前端先从 `/messages`
  水合），然后以 250ms 轮询 `events.jsonl` 把新事件推出去。
- **两路都用真实 event-log seq** 作为 SSE `id:` 行，前端按
  `maxAppliedSeq[sessionKey]` 去重 —— 同一条记录被两路送达时只应用一次。

### 改动

- `storage/fs.py`: `read_session_events_since_with_seq` 返回
  `(seq, payload)` 元组。
- `server/sse.py`: `sse_stream` 接受 dict（自动 seq）或 `(seq, payload)`
  元组（沿用调用方给的 seq），消费者侧解包后再 `_format_event`。
- `server/routes/sessions.py`:
  - `/send` 把每个事件 `append_session_event` 入日志，把真实 seq 作为
    `(seq, ev)` 元组 yield —— **顺带修了一个老 bug**：reconnect 后
    `Last-Event-Id` 之前用的是流内自增计数器，每次新 POST 都从 1
    开始，现在和日志 seq 对齐。
  - 新增 `GET /{sid}/events` 端点。轮询/超时常量提到模块级别
    (`_TAIL_POLL_INTERVAL` / `_TAIL_IDLE_TIMEOUT`) 以便测试缩小。
- `teams/__init__.py`: `_emit_to_lead` 把真实 seq 通过 `_seq` 字段
  附到内存事件上，让 `/send` 期间的 `drain_lead_events` 也能拿到。
- `web/src/lib/sse.ts`: 新增 `streamSessionEvents`（GET + Bearer +
  `Last-Event-Id` 重连）。
- `web/src/pages/Workspace.tsx`: 新增 `eventsAbortRef` 和
  `appliedSeqRef`，挂载时订阅 `/events`，统一调度器 `applyEvent`
  按 seq 去重，daemon 回合里出现的 text/tool_call/done 通过
  `ensureStreamingBubble` 自动创建流式气泡。

### 测试

`tests/test_session_events_tail.py`（8 用例，全过）：
- 存储层 `read_session_events_since_with_seq` 返回元组。
- `sse_stream` 在主迭代和 replay 路径都尊重调用方给的 seq。
- `_format_event` 把 seq 写到 `id:` 行。
- `/events` 带 `Last-Event-Id` 时回放错过的事件、跳过 Last-Event-Id
  那一条。
- `/events` 不带 `Last-Event-Id` 时跳过历史（否则页面挂载会把整个
  transcript 重渲染一遍，每个气泡会双份）。
- `/events` 未知 session → 404。
- `/send` 第一条事件的 `id:` 是日志里的真实 seq，不是流内 1。

端到端冒烟（手写脚本，不在测试套件里）：连接 `/events` 后由后台线程
往日志写事件 —— 验证连接前的事件被跳过、连接后的事件实时推送、seq
是日志里的真值。

### 已知遗留

- `tests/test_inbox_disposition.py::test_dispositions_for_agent_returns_all`
  —— 时间戳精度问题（两条消息同 ts），与本次改动无关。
- `tests/test_mailbox_production.py::test_cross_process_locking_is_attempted`
  —— 本机未装 `portalocker`，与本次改动无关。
