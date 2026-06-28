# 动态 Workflow 模块全面升级设计

> **For Claude:** 本文档是 workflow 模块的 Phase 级升级设计。落地时分阶段执行
> (W1-W6),每阶段独立可合并。需要 Round 2 audit 的 Batch 3(A1 原子写)
> 先落地作为基础。

**生成时间:** 2026-06-28
**当前分支:** `dev-functional`
**关联文档:** `2026-06-28-production-audit-round2.md`(P1 B11)、
`2026-06-28-functional-features.zh.md`(原 workflow 功能描述)

---

## 0. 现状与差距

### 现状

- 工具:`workflow_create` / `_add_step` / `_run_step` / `_run_all` /
  `_status` / `_set_state`(6 个)
- 模型:`Workflow` + `WorkflowStep`,线性步骤列表 + state bag
- 步骤类型:仅 `action`(子 agent 执行 prompt)
- 状态条件:`condition` 字段(Jinja 风表达式)
- 持久化:整个 workflow 单 JSON,无 definition/run 分离
- 并发:同一时刻只有一个 active workflow(B11 锁问题)
- 触发:仅 LLM 主动调用 `workflow_run_all`
- UI:无专属页面,在 Run tab 下的表格里看不到 workflow

### 差距(用户反馈)

| 维度 | 缺失 |
|------|------|
| **审计 / 人工 gate** | 无检查点;步骤无 approve/reject 闸门 |
| **数据约束** | 步骤间无 schema 校验;step 输出直接进 state bag,出错只能事后发现 |
| **触发器** | 无 webhook 入站触发;无 email 接收 / 发送 |
| **复用模型** | 无"模板 vs 实例"分离;跑过的 workflow 不可重用 |
| **运行时可见性** | 无实时事件流;UI 看不到正在跑的 workflow 状态 |
| **长生命周期** | 每个步骤阻塞主线程;checkpoint / trigger_wait 无 parking 机制 |

---

## 1. 概念模型重设计

### 1.1 Definition vs Run 分离

```
WorkflowDefinition (模板,可变,CRUD)
├── metadata: name, description, version, owner, created_at
├── triggers: list[TriggerDef]       # 入口触发器
├── steps: list[StepDef]             # 顺序步骤(可扩展为 DAG)
└── state_schema: JSONSchema         # state bag 校验

WorkflowRun (实例,创建后不可变,持久化)
├── run_id, def_id, def_version
├── status: pending|running|paused|completed|failed|cancelled
├── current_step_idx, started_at, completed_at
├── state: dict                       # 运行时 state bag
├── step_runs: list[StepRun]          # 每步的执行记录
└── events: list[RunEvent]            # 追加型事件日志(给 UI 回放)
```

**为什么分离:**
- Definition 是"流程定义",可版本化、可被 N 个 Run 引用
- Run 是"一次具体执行",有具体输入和结果
- 现有 `wf_id` 混淆了这两者,导致既不能复用也不能审计

### 1.2 步骤类型扩展

| 类型 | 行为 | 关键字段 |
|------|------|---------|
| `action` | 子 agent 执行 prompt | `prompt`, `inputs_schema`, `outputs_schema` |
| `validate` | 确定性校验(无 LLM) | `check`(JSONPath + 谓词,或自定义 fn 名) |
| `checkpoint` | 暂停,等待外部批准 | `approvers`, `timeout_sec`, `on_timeout=approve\|reject\|cancel` |
| `webhook_wait` | 暂停,等待 webhook 触发 | `webhook_id`, `event_filter`, `timeout_sec` |
| `email_wait` | 暂停,等待 email 到达 | `mailbox`, `from_filter`, `subject_filter` |

`action` / `validate` 是同步的;后三者是 **parking step** —— 让出执行线程,事件唤醒后继续。

### 1.3 状态机

```
pending → running → [validate failed] → failed
                  ↓
            step completed → next step
                  ↓
            parking step → paused
                  ↓
            event received → running
                  ↓
            last step done → completed
```

`paused` 状态下 Run 不消耗 CPU,可永久等待(或直到 timeout)。

---

## 2. 执行引擎

### 2.1 WorkflowRunner

```python
class WorkflowRunner:
    """Owns the lifecycle of one WorkflowRun. Drives steps forward, handles
    parking, persists state on every transition."""

    def __init__(self, run: WorkflowRun, storage, dispatcher, executor):
        ...

    async def run(self) -> AsyncIterator[RunEvent]:
        """Drive the run forward, yielding events as they happen. Returns
        when the run reaches a terminal state or parks at a wait step."""
        while not self._is_terminal():
            step = self._current_step()
            if step.type == "action":
                yield from self._run_action(step)
            elif step.type == "validate":
                yield from self._run_validate(step)
            elif step.type in ("checkpoint", "webhook_wait", "email_wait"):
                yield from self._park(step)
                return  # release the coroutine; external event resumes
            self._advance()
```

### 2.2 持久化与恢复

- **每次状态转换都 atomic 写盘**(依赖 A1 修复)
- 服务重启时,扫描所有 `paused` 状态的 run,重新挂回 dispatcher
- `running` 状态的 run 在重启时降级为 `paused`(unfinished step 标记 needs_retry)

### 2.3 调度

- 每个 active run 一个 `asyncio.Task`(不用线程,避免 GIL 竞争)
- 资源限额:per-tenant 最大并发 run 数(默认 10)
- 长任务用 `asyncio.create_task` + cancellation token

---

## 3. 触发器集成

### 3.1 Webhook 入站触发

**复用现有 `WebhookRegistry`,扩展 dispatcher:**

- 每个 `webhook_wait` step 在 run 启动时注册一个 listener:`POST /webhooks/{wf_token}` → 把 payload 推给 runner
- wf_token 与项目级 webhook secret 分开签发,scope 限定到单个 run
- 收到匹配事件 → 唤醒 run,payload 作为 step 的 output

**API:**
- `POST /workflows/runs/{run_id}/webhook/{step_id}` —— 入站端点
- HMAC 签名(同 F7),`?token=<wf_token>` 标识 run

### 3.2 Email 接收

**新模块 `mini_cc/email/`(Phase W4 落地):**

- **入站**:IMAP poller(默认 60s 轮询指定账户)或 LMTP / SMTP receive(生产用)
- **路由**:邮件 To 字段匹配 `wf+<run_id>@<domain>` → 投递到对应 run
- **过滤**:`from_filter`、`subject_filter` 正则匹配
- **出站**:SMTP send(每个 workflow 可选配 SMTP 配置,或共用系统配置)

**部署前提:** 需要 MX 记录指向 mini_cc 节点(收)或外部 SMTP relay(发)。
**MVP 路径:** 不直接收邮件,只发出站邮件 + 接收 webhook(转 email 由外部
bridge 实现,如 SendGrid Inbound Parse)。

### 3.3 触发器列表(definition 层)

definition 的 `triggers` 字段定义"如何启动一个新 run":

```yaml
triggers:
  - type: manual              # UI 点 Run
  - type: webhook             # POST /workflows/{def_id}/trigger
    auth: bearer
  - type: schedule            # 类 cron
    cron: "0 9 * * 1-5"
  - type: email               # wf+<def_id>@domain
    filter:
      from: "@company.com$"
```

触发器匹配后 → 创建新 run + 注入 trigger payload 作为 initial state。

---

## 4. HTTP API

新增 `mini_cc/server/routes/workflows.py`:

```
# Definition CRUD
GET    /workflows                            # list definitions
POST   /workflows                            # create definition
GET    /workflows/{def_id}                   # get definition
PUT    /workflows/{def_id}                   # update (bumps version)
DELETE /workflows/{def_id}

# Run management
POST   /workflows/{def_id}/runs              # start new run
GET    /workflows/{def_id}/runs              # list runs of this def
GET    /workflows/runs                       # list all runs (across defs)
GET    /workflows/runs/{run_id}              # run detail (state + step_runs + events)
DELETE /workflows/runs/{run_id}              # cancel run

# Gate resolution (checkpoint)
POST   /workflows/runs/{run_id}/steps/{step_id}/resolve
       body: { approver, decision: approve|reject, feedback }

# Trigger endpoints
POST   /workflows/{def_id}/trigger           # manual / external trigger
POST   /workflows/runs/{run_id}/webhook/{step_id}  # inbound webhook for waiting step

# Live stream
GET    /workflows/runs/{run_id}/events       # SSE stream of events
```

**Permissions:** 复用现有 `tenant_id` + `scope` 体系;新增 scope:
`workflows:read` / `workflows:write` / `workflows:run` / `workflows:approve`。

---

## 5. 前端重构

### 5.1 Tab 重命名

`Run` → `Workflow`(Workspace.tsx:31 `type Tab`)

旧 Run 表格功能合并进 Workflow tab 的 "Recent Runs" 子面板。

### 5.2 三栏布局

```
┌──────────────────────────────────────────────────────────────────┐
│ Workflow Tab                                                     │
├──────────────┬─────────────────────────────┬─────────────────────┤
│ 左:导航      │ 中:Run 执行视图(chat-like)│ 右:步骤详情        │
│              │                             │                     │
│ ▾ Definitions│ ┌─────────────────────────┐ │ Step: validate      │
│   • release  │ │ [10:01] run started     │ ├─────────────────────┤
│   • research │ │ [10:01] step 1: action  │ │ Status: ✓ passed    │
│   • deploy   │ │   → "found 3 PRs"       │ │ Inputs:             │
│ ▾ Runs       │ │ [10:03] step 2: validate│ │   {pr_count: 3}     │
│   • #42 ✓    │ │   ✓ passed              │ │ Outputs:            │
│   • #43 ⏸    │ │ [10:05] step 3: checkpoint│ │   {valid: true}   │
│   │  awaiting│ │   ⏸ awaiting approval   │ │ Schema:             │
│   │ approval │ │   [Approve] [Reject]    │ │   (jsonschema)      │
│   • #44 ▶    │ │                         │ │                     │
│              │ └─────────────────────────┘ │                     │
└──────────────┴─────────────────────────────┴─────────────────────┘
```

- **左栏:** 双层 tree(Definitions / Runs),按 def 过滤 runs,实时状态徽章
- **中栏:** 类 chat 的纵向事件流,checkpoint 在流内提供 Approve/Reject 按钮
- **右栏:** 选中步骤的 inputs/outputs/schema/approver 信息

### 5.3 实时事件流

- 中栏通过 SSE 订阅 `/workflows/runs/{run_id}/events`
- 复用现有 SSE bridge(但需要 A6 心跳 + A5 maxsize)
- 事件类型:`run_started` / `step_started` / `step_completed` /
  `step_failed` / `step_paused` / `gate_resolved` / `run_completed`

### 5.4 Definition 编辑器

- **Phase W1-W5:** JSON 编辑(TextInput),够用即可
- **Phase W6:** 可视化拖拽编辑器(可选,优先级低)

---

## 6. 工具(模型侧)兼容

现有 6 个 `workflow_*` 工具保留,但内部映射到新模型:

| 旧工具 | 新行为 |
|--------|--------|
| `workflow_create` | 创建 definition(无 trigger),返回 def_id |
| `workflow_add_step` | 给最新 definition 加步骤 |
| `workflow_run_all` | 创建 run + 启动 + 阻塞至完成或 pause |
| `workflow_run_step` | 仅用于手动重试,需要 run_id |
| `workflow_status` | 返回 run 状态 + 当前 step |
| `workflow_set_state` | 修改 run.state(仅 paused 时允许) |

**新增工具:**
- `workflow_add_checkpoint_step(name, approvers, timeout_sec)`
- `workflow_add_validate_step(name, check_expr)`
- `workflow_list_runs(def_id?)`
- `workflow_resolve_gate(run_id, step_id, decision, feedback)`

---

## 7. 分阶段落地

| Phase | 范围 | 依赖 |
|-------|------|------|
| **W1** | Definition/Run 分离 + 新存储模型 + 手动触发 + 基础 API + 最简 UI 列表 | A1(原子写) |
| **W2** | Checkpoint step + Gate 解决 API + UI Approve/Reject | W1 |
| **W3** | Webhook 入站触发(step-level + definition-level) | W1 |
| **W4** | Email 子系统(SMTP 出站 + IMAP 入站 poll) | W1,部署侧 MX 配置 |
| **W5** | Validate step + JSON Schema 约束 + 错误降级路径 | W1 |
| **W6** | 可视化 Definition 编辑器 + UX 抛光 | W1-W5 |

**每个 Phase 一个 session,独立 commit + push。** W4 最大(新模块),
W6 最重(前端),其余相仿。

---

## 8. 风险与权衡

| 风险 | 缓解 |
|------|------|
| **长生命周期 run 占内存** | paused run 不持协程,只占盘;running run 每个一个 asyncio.Task,有上限 |
| **Webhook 入站安全** | per-run wf_token + HMAC;URL 不泄露 token 时限短 TTL(默认 24h) |
| **Email 部署复杂** | Phase W4 先做 outbound;inbound 默认 IMAP poll,生产用 LMTP 由运维部署 |
| **数据迁移** | 现有 `wf_id` 平迁到新 schema;提供 `migrate_workflows.py` 脚本 |
| **UI 复杂度** | 三栏布局对小屏不友好;响应式下右栏可折叠 |
| **并发 run 数失控** | per-tenant 上限(默认 10),超过则新 run 排队 |

---

## 9. 不在本设计范围

- **DAG 步骤依赖**(本期保持线性;条件 `condition` 字段保留作软分支)
- **跨 workflow 调用**(workflow 嵌套)
- **工作流市场 / 分享**(definition 分享机制)
- **历史 run 数据归档**(默认无限保留;归档策略后续提)
- **多租户 quota 计费**(scope 控制 + 计数在 metrics,但不算费)
