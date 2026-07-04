# Phase I — Team Orchestration: Lead 全程感知 + 自主协作

> **For Claude:** 本文档是 Phase I 设计 + 实施计划。每节先讲设计意图再给任务清单。
> 实施时按 I.A → I.B-1 → I.B-2 → I.C 顺序推进，每个 Task 走 TDD red→green→commit。

**Goal:** 让用户能"派 3 个 teammate 自主干活、互相讨论"，lead 全程感知关键进展，UI 把所有 teammate 活动铺开供用户随时查看。

**Architecture:** 三层并进 —— (1) 消息分级：teammate 间 chitchat 不打扰 lead，milestone/blocker 自动 CC lead；(2) Lead auto-wake：milestone 落地后 debounce 5s 触发 lead turn，用户不在场也持久化；(3) Observability：聚合 endpoint + TeammatesPanel 升级 + 主面板可切"team 视角"。

**Tech Stack:** Python (FastAPI, asyncio, threading) / React + TypeScript + zustand / 现有 MessageBus + WakeupScheduler + events.jsonl 持久化。

**生成时间:** 2026-07-03
**前置状态:** `dev-functional` 分支，1252 测试通过。Teammate 自主跑/互发消息/领任务已就绪；lead 仅在用户输入时触发；UI 仅显示 alive/stopped 状态。

---

## 0. 背景与设计原则

### 当前缺口（来自用户实测 + 代码盘点）

| 维度 | 现状 | 问题 |
|---|---|---|
| Teammate ↔ Teammate | ✅ `bus.send("alice", "bob", ...)`，bob `_idle_poll` 5s 内捡到 | 没问题 |
| Teammate 自主领任务 | ✅ `_scan_unclaimed_tasks` | 没问题 |
| Lead 看到所有 teammate 间对话 | ❌ 只能看到 CC 给 lead 的 | "全程感知"失败 |
| Lead 反应及时性 | ⚠️ Lead 只在用户输入时 wake；milestone 进 mailbox 但 lead 不知道要 wake | 用户必须"敲一下"lead 才反应 |
| UI 显示 teammate 当前活动 | ❌ 只显示 alive/stopped + role + age + inbox count | 用户看不到 teammate 在干嘛 |
| UI 显示 teammate 间对话 | ❌ 缺 | 同上 |

### 三层并进的依据

"Lead 全程感知"是模糊需求。区分两种含义：

- **LLM context 全程感知**：lead 的 `loop.messages` 装下所有 teammate 活动 → context 爆炸 + token 成本 + LLM 被噪音带偏主线。**不推荐**。
- **可观测性全程感知**：lead 的 LLM 只看 milestone/blocker；用户通过 UI 看所有活动。**推荐**。

Phase I 走第二种 —— 让**用户**全程感知细节，让 **lead** 在关键节点被叫醒。

### 三大支柱

```
┌─ I.A 消息分级 ────────────────────────────────────────┐
│  bus.send 时 if msg_type in {result,milestone,blocker} │
│  → 自动复制到 lead mailbox                             │
│  → lead LLM context 只接收关键消息                     │
└──────────────────────────────────────────────────────┘
              │
              ▼ milestone/blocker 落地
┌─ I.B Lead auto-wake ─────────────────────────────────┐
│  Lead mailbox watcher daemon (per-project thread)    │
│  debounce 5s → 起 lead AgentLoop turn                 │
│  turn 输出 → events.jsonl + _lead_events deque        │
│  用户在场：SSE 实时推送；不在场：refresh 回放          │
└──────────────────────────────────────────────────────┘
              │
              ▼ teammate 任何活动
┌─ I.C Observability ──────────────────────────────────┐
│  GET /projects/{pid}/team/activity?since=<ts>         │
│  TeammatesPanel: 每个 row 展开看最近 N 条 events      │
│  Workspace: 加 "team 视角" tab，时间线合并展示         │
└──────────────────────────────────────────────────────┘
```

### 阶段化策略

| 阶段 | 内容 | 工程量 | 风险 |
|---|---|---|---|
| **I.A** | 消息分级 + auto-CC lead | 小（~1 天） | 低，纯 bus 层逻辑 |
| **I.B-1** | Lead in-stream auto-wake（用户在场时） | 中（~2 天） | 低，复用 `_inject_teammate_replies` 路径 |
| **I.B-2** | Lead out-of-stream auto-wake（用户不在场） | 大（~3 天） | 中，要起 daemon thread + 并发控制 |
| **I.C** | Observability endpoint + UI 升级 | 大（~3 天） | 低，纯 UI/endpoint |

实施顺序：I.A → I.B-1 → I.B-2 → I.C。每个阶段独立可发布，I.B-2 不做也能 ship（lead 退化为"用户在场时自动反应"）。

---

## I.A — 消息分级与 Auto-CC Lead

### 设计

扩展现有 `msg_type` 字段的语义。`MessageBus.send` 在写入 to-recipient 的同时，如果 msg_type 属于"lead 关注类"，复制一份到 lead mailbox + 触发 `_lead_hook`。

**关注的 msg_type 集合（`_LEAD_CC_TYPES`）**：

| msg_type | 含义 | 触发场景 |
|---|---|---|
| `result` | 子任务完成 | teammate 完成 lead 派的活 |
| `milestone` | 阶段性进展 | teammate 完成重要中间步 |
| `blocker` | 卡住/需要 lead 决策 | teammate 无法继续 |

不进 lead mailbox 的：`message`（chitchat）、`mention`（已通过别的路径）、`shutdown_request`、`plan_*`（协议消息走原路径）。

**为什么要 CC 而不是让 teammate 显式 send 两次**：teammate 写代码时只用 `send_message(to="bob", msg_type="result", ...)`，不需要每次记得加一份给 lead。语义上"result 是 milestone，lead 该知道"由 bus 层强制。

### Task I.A.1: msg_type 分级常量 + 测试

**Files:**
- Modify: `mini_cc/teams/__init__.py:66` (`MessageBus.__init__`)
- Test: `tests/test_message_bus_lead_cc.py` (new)

**Step 1: 写失败测试**

```python
# tests/test_message_bus_lead_cc.py
"""Phase I.A: teammate→teammate messages with msg_type in
{result, milestone, blocker} auto-CC lead so the lead's mailbox
sees them without the teammate explicitly addressing lead."""
from mini_cc.teams import MessageBus

_LEAD_CC_TYPES = ("result", "milestone", "blocker")


def test_result_message_between_teammates_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "task done", msg_type="result")
    assert any(m["content"] == "task done"
               for m in bus.peek_inbox("lead"))


def test_milestone_message_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "milestone hit", msg_type="milestone")
    assert bus.peek_inbox("lead"), "milestone must CC lead"


def test_blocker_message_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "stuck on X", msg_type="blocker")
    assert bus.peek_inbox("lead")


def test_plain_chitchat_does_not_cc_lead(tmp_path):
    """Generic 'message' between teammates stays private."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "what do you think?", msg_type="message")
    assert bus.peek_inbox("lead") == []


def test_cc_does_not_duplicate_when_recipient_is_lead(tmp_path):
    """If lead is already the direct recipient, don't double-write."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "lead", "done", msg_type="result")
    assert len(bus.peek_inbox("lead")) == 1


def test_cc_fires_lead_hook_once(tmp_path):
    """Lead hook fires exactly once per send, even on CC path."""
    fired: list[dict] = []
    bus = MessageBus(tmp_path / "ws")
    bus.set_lead_hook(lambda m: fired.append(m))
    bus.send("alice", "bob", "done", msg_type="result")
    assert len(fired) == 1
    assert fired[0]["content"] == "done"
```

**Step 2: 跑测试确认 FAIL**

```bash
.venv/Scripts/python.exe -m pytest tests/test_message_bus_lead_cc.py -v
```
预期：所有 assert 失败，因为 bus 还没实现 CC 逻辑。

**Step 3: 实现 CC 逻辑**

修改 `mini_cc/teams/__init__.py:248` 的 `MessageBus.send`：

```python
# In MessageBus class — add near _HISTORY_CAP
_LEAD_CC_TYPES: frozenset[str] = frozenset(
    {"result", "milestone", "blocker"}
)

def send(self, from_agent: str, to_agent: str, content: str,
         msg_type: str = "message",
         metadata: dict | None = None) -> dict:
    msg = {"from": from_agent, "to": to_agent,
           "content": content, "type": msg_type,
           "ts": time.time(), "metadata": metadata or {}}
    with self._lock:
        self._cache.setdefault(to_agent, []).append(msg)
        self._append_disk(self._path(to_agent), msg)
        # ... history append stays ...

    # Phase I.A: auto-CC lead on milestone-class messages. Don't
    # duplicate if lead is already the direct recipient.
    cc_lead = (to_agent != "lead"
               and msg_type in self._LEAD_CC_TYPES)
    if cc_lead:
        cc_msg = {**msg,
                  "to": "lead",
                  "metadata": {**(metadata or {}),
                               "cc": True,
                               "original_to": to_agent}}
        with self._lock:
            self._cache.setdefault("lead", []).append(cc_msg)
            self._append_disk(self._path("lead"), cc_msg)
            hist = self._history.setdefault("lead", [])
            hist.append(cc_msg)
            if len(hist) > self._HISTORY_CAP:
                del hist[0:len(hist) - self._HISTORY_CAP]
            self._append_disk(self._history_path("lead"), cc_msg)

    # Lead hook (existing) — fires once for the original msg, OR
    # once for the CC. Lead hook only cares about messages lead
    # will see, so prefer the CC copy when present.
    target_for_hook = cc_msg if cc_lead else msg
    if (target_for_hook.get("to") == "lead"
            and self._lead_hook is not None):
        try:
            self._lead_hook(target_for_hook)
        except Exception:
            pass
```

**Step 4: 跑测试 PASS**

**Step 5: 回归**

```bash
.venv/Scripts/python.exe -m pytest tests/test_p0_teams.py tests/test_phaseH_agents_workflow.py tests/test_teams_broadcast.py tests/test_lead_mailbox_inject.py
```

**Step 6: 提交**

```
feat(teams): auto-CC lead on result/milestone/blocker messages

Phase I.A — teammate→teammate messages with msg_type in {result,
milestone, blocker} now copy into lead's mailbox automatically.
Plain chitchat (msg_type=message) stays private. Closes the
"lead has no idea teammates made progress" gap that required the
user to manually nudge lead with check_inbox.
```

---

## I.B — Lead Auto-Wake

### I.B-1: In-Stream Auto-Wake（用户在场时）

**设计**：用户的 `/send` 在跑 → lead loop 在跑 → mailbox watcher 检测到 milestone/blocker → 标记下一次 iteration 必须再跑一个 turn（绕过 `_stop` set）。

最简实现：用 `WakeupScheduler` —— mailbox 收到 milestone 时，schedule 一个 5s wakeup。`_inject_cron_fired` 已经会消费 wakeup 并把它作为下一轮 user 消息注入。这样 lead 的 loop 自然会多跑一轮。

**问题**：`_stop` 在 LLM stop_reason=end_turn 时被 set，loop 退出。需要让 wakeup 翻新 `_stop`。或者更干净：watcher 检测到 milestone 时直接调用 loop 的"再跑一轮"入口。

**方案**：给 `AgentLoop` 加 `nudge(content: str)` 方法 —— 重置 `_stop`、把 content 作为新 user message append、signal loop 继续。Watcher 在 debounce 后调用。

### Task I.B-1.1: AgentLoop.nudge 入口 + 测试

**Files:**
- Modify: `mini_cc/core/loop.py:AgentLoop`
- Test: `tests/test_loop_nudge.py` (new)

**Step 1: 写失败测试**

```python
# tests/test_loop_nudge.py
"""Phase I.B-1: lead's AgentLoop exposes a nudge() entry point so a
watcher can re-enter the loop with new content after the previous
turn ended, without going through /send."""
# Reuse _Block/_MockClient/_build_loop from test_lead_mailbox_inject
# (or import them via fixture)


def test_nudge_appends_user_message_and_resumes(tmp_path):
    """After loop.run() returns, calling nudge(content) should
    re-enter the loop and emit a turn that consumes the nudged
    content."""
    script = [
        _MockResponse([_Block(type="text", text="first")]),
        _MockResponse([_Block(type="text", text="after nudge")]),
    ]
    loop, _ = _build_loop(tmp_path, script)
    # First turn
    list(loop.run("hi"))
    assert "first" in str(loop.messages)
    # Nudge — should trigger another turn
    loop.nudge("[Teammate milestone] alice done")
    # ... join the background thread started by nudge ...
    assert "alice done" in str(loop.messages)
    assert "after nudge" in str(loop.messages)
```

**Step 2-5: TDD green** — 实现 `nudge`：

```python
# mini_cc/core/loop.py — AgentLoop method
def nudge(self, content: str) -> None:
    """Re-enter the loop with a new user message. Used by Phase I.B-1
    watcher to wake lead after a teammate milestone lands. Idempotent
    if loop is already running — appends to messages and the running
    iteration's iteration will see it on next _inject_* pass.

    For idle lead (no /send active), spawns a fresh run in a worker
    thread."""
    self._stop.clear()
    self.messages.append({"role": "user", "content": content})
    if not self._running:
        t = threading.Thread(target=self._run_until_idle,
                             daemon=True)
        t.start()
```

需要加 `_running` 标志 + `_run_until_idle` helper。

### Task I.B-1.2: Mailbox watcher daemon

**Files:**
- Modify: `mini_cc/teams/__init__.py:TeammateSpawner`
- Test: `tests/test_lead_watcher.py` (new)

**设计**：

```
LeadWatcher (per project)
    thread loop:
        poll lead mailbox every 1s
        if pending milestone/blocker:
            wait 5s (debounce)
            drain mailbox
            nudge(loop, "[Teammate milestone]\n" + json.dumps(pending))
```

Polling 比 condition variable 简单且对 crash 更鲁棒。5s debounce 避免 3 个 teammate 同时报 result 起 3 个 turn。

### Task I.B-1.3: 路由 watcher 事件到当前 /send 流

如果 /send 在跑（用户在场），nudge 触发的 turn 的 events 要走当前 SSE 流。复用 `bind_event_sink` 机制。

### I.B-2: Out-of-Stream Auto-Wake（用户不在场时）

**设计**：用户没在 `/send` —— lead loop 仍由 watcher 触发，但 events 走 `events.jsonl` 持久化，下次 refresh 回放。**这一阶段不需要新代码，只是 I.B-1 的副产物**——nudge 起的 turn 无论 /send 在不在都跑，输出走 events.jsonl 即可。

风险：用户正在打字时 lead 起一个 turn，可能并发写 `loop.messages`。**Task I.B-2.1** 加 `loop.messages` 写锁（threading.RLock）。

---

## I.C — Observability

### Task I.C.1: 后端聚合 endpoint

**Files:**
- Create: `mini_cc/server/routes/team.py` (new)
- Modify: `mini_cc/server/app.py` (register router)
- Test: `tests/test_team_activity_endpoint.py` (new)

**设计**：

```
GET /tenants/{tid}/projects/{pid}/team/activity?since=<iso_ts>&limit=200
    → 200 { events: [{ session_id, ts, type, ... }],
            has_more: bool }
```

读取 `<project>/.storage/<session>/events.jsonl`，过滤 `ts > since`，合并 sort by ts，截断 limit。支持 `?teammate=alice` 单一过滤。

### Task I.C.2: 前端 Activity Store + 轮询

**Files:**
- Create: `mini_cc/web/src/lib/teamActivity.ts` (new)
- Modify: `mini_cc/web/src/lib/types.ts` (add TeamEvent type)

zustand store：`perProjectActivity: Record<pid, TeamEvent[]>`、`since: Record<pid, number>`、`poll(pid)`。

### Task I.C.3: TeammatesPanel 升级 — 每个 row 可展开看最近 events

**Files:**
- Modify: `mini_cc/web/src/components/TeammatesPanel.tsx`

UI：
- 当前 row（avatar + name + role + status）保留
- 展开时显示最近 10 条 events（tool_use/tool_result/send_message），来源 `team/activity?teammate=<name>`
- **实现说明（与原计划的偏差，2026-07-04）**：原计划「`/agents inbox` polling 改用 `team/activity?teammate=<name>` 替代」在落地时发现：inbox peek 返回的 `CardListItem` 形状是 ack/ignore 按钮的依赖（按钮按 message id 操作 inbox 消息），而 activity events 不携带 message id —— 完全替换会破坏消息处置流。最终方案是**双源**：roster（含 alive/status）继续来自 `/agents`，新增 activity 区块来自 `/team/activity`，inbox peek（含 ack/ignore）保留在 activity 下方。两个 4s 轮询并行；future work 可考虑放慢 roster 轮询至 8-10s。
- 默认 4s 轮询 teammate activity（与 `/agents` roster 轮询并行）

### Task I.C.4: 主面板新增 "Team" tab（可选，高 ROI）

**Files:**
- Modify: `mini_cc/web/src/pages/Workspace.tsx`

在 chat / files / run 旁边加 "team" tab。点开后显示：
- 时间线视图（按 ts 合并所有 teammate + lead events）
- 可按 teammate 过滤
- 可点击 event 跳到对应 session

### Task I.C.5: 主 chat 中插入"teammate activity"轻提示（可选）

**Files:**
- Modify: `mini_cc/web/src/pages/Workspace.tsx`

当 watcher 触发 lead turn（来自 nudge）时，主 chat 出现一条灰色通知"Alice reported a milestone → lead is responding..."。低噪音高可观测。

---

## 2. 风险与开放问题

### 风险

1. **Lead context 爆炸**：milestone CC 是好事，但若有 5 个 teammate 每人每小时报 3 个 milestone = 15 条/小时进 lead context。**对策**：CC 写 mailbox，`_inject_teammate_replies` 只在 turn start drain 一次；考虑给 milestone 加摘要（teammate 发 1 句话 + 详情链接）。

2. **Nudge 并发**：用户 /send 和 watcher nudge 同时跑同一个 loop.messages → race。**对策**：Task I.B-2.1 加 RLock，写 messages 都过锁。

3. **无限循环**：lead 收到 milestone → 醒来 → 调用 teammate → teammate 完成报 milestone → lead 再醒 → ...。**对策**：给 lead 一条"已经处理过这个 milestone"的 dedup 逻辑（基于 metadata.id）；给 teammate 的任务完成加 cooldown。

4. **Daemon thread 生命周期**：ProjectLeader watcher thread 何时起、何时停？**对策**：在 ProjectManager 里随项目加载/卸载；进程退出时 daemon=True 自动清理。

### 开放问题（请决定）

1. **CC 是否包含 `mention` 类型？** 当前 mention 走 `route_mentions` 直接进 teammate mailbox；如果 alice 给 bob mention，lead 该看到吗？
2. **Nudge 输出的 UI 行为**：watcher 触发的 lead turn，UI 应该 (a) 立即冒一个 lead bubble，(b) 只更新 Team tab 的活动流，(c) 显示通知？
3. **Debounce 5s 是否合理？** 短了浪费 LLM 调用，长了用户等不及。可配置吗？

---

## 3. 验收标准

Phase I 完成后，以下场景应该全过：

### 场景 A：3 teammate 协作无 lead 介入

1. 用户 spawn alice/bob/carl，派一个含 3 个子任务的总任务
2. 关闭浏览器
3. 5 分钟后回来
4. 打开主 session：lead 已 auto-wake 多次，记录了每个 teammate 完成 milestone
5. TeammatesPanel 显示 3 个 teammate 各自最后活动
6. Team tab 显示完整时间线

### 场景 B：teammate blocker 唤醒 lead

1. alice 在执行中卡住（要 lead 决策）
2. `send_message(to="lead", msg_type="blocker", ...)`
3. 5s 内 lead auto-wake，看到 blocker，回复 alice
4. alice 收到回复继续

### 场景 C：teammate chitchat 不打扰 lead

1. alice 问 bob "你这个文件用的是什么 schema？"
2. bob 回复
3. **lead 不 wake**，lead mailbox 不收到任何东西
4. UI 的 Team tab 能看到这段对话
5. Lead 下次被 nudge 时也不会看到这段（除非显式 `history` 查询）

### 场景 D：用户在场时 milestone 实时显示

1. 用户在主 session 输入"alice 进度如何"
2. lead 调 `check_inbox` → 看到 alice milestone → 回复
3. 此时 bob 也完成 milestone → UI 立刻显示 "Bob reported: ..." 通知
4. 5s 后 lead auto-wake 处理 bob 的 milestone

---

## 4. 实施顺序与提交节奏

每个 Task 独立 PR/commit：

```
I.A.1  消息分级 + auto-CC lead                       [~1d]
─── 阶段 1 ship-able：lead mailbox 自动捕获 teammate 关键消息 ───

I.B-1.1  AgentLoop.nudge() 入口                     [~0.5d]
I.B-1.2  Mailbox watcher daemon                     [~1d]
I.B-1.3  SSE 路由                                    [~0.5d]
─── 阶段 2 ship-able：用户在场时 lead 自动响应 milestone ───

I.B-2.1  loop.messages 写锁                          [~0.5d]
─── 阶段 3 ship-able：用户不在场也跑（ durable ） ───

I.C.1  后端聚合 endpoint                            [~0.5d]
I.C.2  前端 Activity store + 轮询                   [~0.5d]
I.C.3  TeammatesPanel 升级                          [~1d]
I.C.4  Team tab（可选）                              [~1d]
I.C.5  通知 toast（可选）                            [~0.5d]
─── 阶段 4 ship-able：用户全程感知所有 teammate 活动 ───
```

总计 ~7-8 工作日，分 4 个里程碑发布。

---

## 5. 引用

- 现状代码：
  - `mini_cc/teams/__init__.py:248` — `MessageBus.send`
  - `mini_cc/teams/__init__.py:693` — `_emit_to_lead`
  - `mini_cc/teams/__init__.py:889` — teammate `_idle_poll`
  - `mini_cc/core/loop.py:474` — main while-loop
  - `mini_cc/core/loop.py:783` — `_inject_teammate_replies`（Phase H 已 ship）
- 相关历史 fix：
  - `b7f1866` — scope lead event deque to bound session
  - `51ab21d` — drain lead mailbox into context so late replies reach LLM
- 相关 skill：`@superpowers:test-driven-development`、`@superpowers:executing-plans`、`@superpowers:systematic-debugging`

---

## 6. Post-implementation UI rendering fixes

> 本节追加于 Phase I 主线合并后的 e2e 验证轮（2026-07-04 ~ 07-05）。设计层不变，但 Team tab 在真实 LLM 输出下暴露了若干渲染层问题，记录于此便于后续维护。

### 6.1 流式文本按 token 一行（commit `247db17`）

**症状**：Team tab 每条消息按 SSE 流的 token chunk 一行一行展开，无法阅读。

**根因**：`AgentLoop` 每个 token 都 emit 一个 `text` SSE 事件，`/send` 把它们逐条写进 `events.jsonl`。渲染层直接平铺显示，结果一行一 token。

**修复**：`mini_cc/web/src/lib/teamEvent.ts` 加 `mergeConsecutiveTexts()` helper —— 把同 session 的相邻 `text` 事件合并成一个 `merged_text` 渲染项。合并发生在渲染层而非持久化层，可同时修复历史日志。

### 6.2 合并方向、speaker 标签、inbox 友好化（commit `94581bf`）

**症状（3 个相互独立）**：

1. 合并后的气泡从右往左读 —— timeline 把活动流先 reverse 再 merge，每个气泡内的 chunk 被按"最新在前"拼接。
2. `teammate_message` 事件显示在 LEAD session 下，右栏 speaker 标签写成 "lead"，但实际说话人是 teammate。
3. teammate session 的气泡里直接渲染 `<inbox>[{"from":"lead",...}]</inbox>` 原始 JSON。

**修复**：

- merge 顺序：先在 ascending 流上 merge，再 reverse 渲染项 —— 气泡内文字恢复从左到右。
- 新增 `speakerLabel(e)` helper：优先取事件的 `from` 字段，与 host session 不同时用 `from`，否则回退 `sessionLabel`。
- 后端 `mini_cc/teams/__init__.py:_format_inbox_as_dialogue()` 把 inbox JSON dump 转成「N messages for @alice: [1] lead said (type: milestone): ...」格式的人话，原始 JSON 保留为 HTML 注释备份。

### 6.3 teammate_message 占位符 + 信封幻觉（commit `75ab087`）

**症状（2 个）**：

1. alice/bob/carl 的事件全部显示成 `[teammate_message]` 占位符，看不到实际台词。
2. lead 气泡里出现 `<teammate_messages>...</teammate_messages>`、`<function_calls><invoke>...</invoke></function_calls>` 等原始 XML 信封。

**根因**：

- `summarizeEvent` switch 缺 `teammate_message` case，走 default 的 `[${type}]` 占位符。
- LLM 偶尔把自己的 input/tool-call 协议（XML 信封）当文本吐出来。SSE 流把标签切成 3-4 字符的小 chunk（`<te` / `amm` / `ate` / `_messages` ...），单 chunk 内永远凑不齐完整标签名。

**修复**：

- `summarizeEvent` 加 `teammate_message` case：直接渲染 `content`（截断 80 字，msg_type 作 fallback）。
- `mergeConsecutiveTexts` 在 flush 时对**合并后整段文本**做 `includes` 检查；只要出现 `HALLUCINATED_ENVELOPE_TAGS`（`teammate_messages` / `inbox` / `system_messages` / `channel_update` / `notifier` / `function_calls` / `invoke` / `parameter`）任一开/闭标签，整泡丢弃。

**保守取舍**：若一泡里既有真实文本又有信封标签，整泡丢。理由：实际数据里这种混合极少；信封出现就是 LLM 在乱吐协议，整泡多半是垃圾，可靠隐藏比外科手术式救援重要。

**跨泡场景**：当 `teammate_message` 事件打断了信封幻觉（LLM emit 信封开标签 → 真事件落地 → LLM 继续吐闭标签 + JSON 碎片），后段气泡里只有闭标签没有开标签。"任一标签 → 丢弃" 规则闭标签也命中，所以尾巴也会被吞掉。

### 6.4 测试覆盖

新增 `mini_cc/web/src/lib/teamEvent.test.ts`，共 **22 个单测**：

- `mergeConsecutiveTexts` × 7：合并、跨 session 切分、非文本事件打断、空输入、首 chunk 时间戳、流顺序保持、信封剥离（4 个 subcase + 跨泡尾巴 1 个）。
- `speakerLabel` × 5：普通文本、lead session、`from` 字段优先、与 host 同名时回退、缺失时回退。
- `summarizeEvent — teammate_message` × 3：内容渲染、长内容截断、缺失内容回退。

### 6.5 e2e 验证（相声团队场景）

为完整覆盖"3 teammate 自主表演"的渲染路径，新增 `mini_cc_data_xiangsheng/` 数据目录（fresh tenant `xs` + project `xs_demo`），让 lead spawn 逗哏/捧哏/泥缝三人表演《扒马褂》片段。修复前 80 行 timeline 里有 `[teammate_message]` 占位符 + 多个信封幻觉泡；修复后 80/80 全是干净对话行。

> **注**：`mini_cc_data_xiangsheng/` 与 `xs-team-tab-*.png` 截图一并入库，便于回放验证。生产部署时不应依赖这些数据。

