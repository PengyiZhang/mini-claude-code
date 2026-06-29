[ < [09](09-auth.md) ] [ [11](11-mcp-plugins.md) > ] · [English version](../en/10-workflow-v2.md)

# 10 — 工作流 V2

> 旧版 `Workflow` 把"要做什么"(模板)和"做完了什么"(状态+结果)塞在同一个可变
> 对象里。V2 拆成三段：`WorkflowDefinition`(版本化模板)、`WorkflowRun`(单次
> 执行)、`StepRun`(单步记录)。再加上 `def_snapshot` 锚定、`{step_id}` 占位符
> 替换、5 种 step 类型,工作流就能像 Airflow 那样"暂停 → 外部事件恢复"地跑,
> 同时保持纯函数式的可复现性。

---

## 问题与动机

s20 的 `tools/workflow.py` 是个把 `prompt + state + results` 全揉一起的"胖
dataclass"。在多人协作的后端场景下三个问题立刻爆开:

1. **模板和状态混在一起**。你想改一个 step 的 prompt,就动了 `state`;想重跑
   失败的一步,发现 `results` 已经被覆盖。模板和执行历史根本不该共享可变性。
2. **没有"人审"或"等外部回调"这种语义**。生产里 90% 的"工作流"都需要在中间
   一步暂停——等审批、等 GitHub webhook、等用户回邮件。旧版只能一口气跑完,
   要做 human-in-the-loop 得自己在应用层 hack 一个 state machine。
3. **改定义会把运行中的 run 带偏**。`def_version` 标签写是写了,但旧版执行时
   直接读 live 定义,你中途 PUT 一次定义,正在跑的 run 突然多了一步/少了一步。

V2(`mini_cc/workflow_v2.py`)按 W1~W6 的节奏解决这三件事:Definition/Run
分离(W1)、checkpoint 人工门(W2)、webhook_wait 门(W3)、email_wait 门(W4)、
validate 确定性检查(W5)、UI drive 端点 + `{step_id}` 占位符(W6),以及
`def_snapshot` 在 run 创建时把定义冻结。

---

## 设计与原理

### 1. 三层数据模型

```
WorkflowDefinition  (版本化模板,PUT 时 version++)
   │  def_id, version, steps: [StepDef...], triggers, state_schema
   │
   │  start_run() —— 拷贝当前 def 到 def_snapshot,新建 WorkflowRun
   ▼
WorkflowRun  (单次执行)
   │  run_id, def_id, def_version, status, current_step_idx
   │  state, step_runs: [StepRun...], def_snapshot  ←─ 冻结的定义
   │
   │  每个 step 完成后追加一个 StepRun
   ▼
StepRun  (单步记录,不可变追加)
   step_id, status, started_at, completed_at, output, error
```

`WorkflowDefinition` 只通过 CRUD 改,每次 PUT `version+1`(`workflow_v2.py:245`);
`WorkflowRun` 由 `start_run` 从某个 def 创建,记录每一步的 `StepRun`。三层各
自持久化,`to_dict/from_dict` 全对称。

### 2. def_snapshot:运行中改定义不会污染 run

`start_run` 时把当时定义拷一份存到 `run.def_snapshot`(`workflow_v2.py:175`,
`:269`)。`drive_run` 和三个 `resolve_*` 都通过 `_resolve_run_def` 取这份快照,
而不是 live def:

```python
def _resolve_run_def(self, project_id, run):
    if run.def_snapshot:
        return WorkflowDefinition.from_dict(run.def_snapshot)
    return self.get_definition(project_id, run.def_id)   # legacy 回退
```

(`workflow_v2.py:280`)。没有这一步,你在 run 跑到一半时改定义、PUT,
`drive_run` 的下一次循环会突然迭代新 steps —— `def_version` 标签就成了谎言。

### 3. drive_run:5 种 step 类型的状态机

`drive_run(dispatch_fn, max_steps=1000)`(`workflow_v2.py:315`)按 `current_step_idx`
推进,每种 step 行为不同:

| Step 类型       | 行为                                                                      |
|-----------------|---------------------------------------------------------------------------|
| `action`        | 调 `dispatch_fn(prompt, run)`(走 AgentLoop),返回文本存入 `run.state[step_id]` |
| `validate`      | 不调 LLM,对 state 跑 `_run_validate`(安全 eval 或 JSON schema)           |
| `checkpoint`    | 翻 `status="paused"`,`StepRun.status="paused"`,return                     |
| `webhook_wait`  | 同 checkpoint,等 `resolve_webhook_wait` 喂 payload                       |
| `email_wait`    | 同 checkpoint,等 `resolve_email_wait` 喂邮件                              |

驱动循环骨架:

```python
for idx in range(run.current_step_idx, min(len(d.steps), max_steps)):
    step = d.steps[idx]
    if step.type in ("checkpoint", "webhook_wait", "email_wait"):
        run.status = "paused"; run.current_step_idx = idx
        sr.status = "paused"; ...; return run      # parking
    if step.type == "validate":
        valid, detail = self._run_validate(step, run.state); ...
    # else action
    scope = {**run.state, "step_id": step.id}       # NEW: {step_id} 注入
    prompt = self._substitute(step.prompt, scope)
    result = dispatch_fn(prompt, run)
    run.state[step.id] = result
```

(`workflow_v2.py:341-412`)

### 4. {step_id} 占位符替换(NEW)

每个 action step 的 `prompt` 支持 `{name}` 形式占位符,从 `run.state` 取值。
新加的保留键 `step_id`(`workflow_v2.py:394`)让模板可以写"现在正在跑 step
`{step_id}`,请基于上一步 `step_xxx` 的结果继续"。替换逻辑在 `_substitute`
(`workflow_v2.py:575`):

```python
@staticmethod
def _substitute(prompt, scope):
    def repl(m):
        v = scope.get(m.group(1))
        return str(v) if v is not None else m.group(0)   # 未找到原样保留
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, prompt)
```

找不到的 key 原样保留 `{xxx}`,而不是崩——这样 prompt 里出现的 JSON 片段不会被
误吞。

### 5. validate 的安全 eval

`_run_validate`(`workflow_v2.py:520`)支持两种 check:

- 字符串表达式:`{"check": "len(items) <= 10 and count > 0"}`,用 `eval` 但
  `__builtins__` 锁死在 `_VALIDATE_SAFE_BUILTINS`(只有 len/str/int/min/max/
  sorted/isinstance 等白名单,见 `workflow_v2.py:510`)。
- JSON schema:`{"check": {"schema": {"type": "object", "required": ["x"]}}}`,
  走 `_validate_schema` 做 type + required 的最小实现(装 `jsonschema` 包才会
  上完整校验)。

关键点:**bool 不是 int**。`_validate_schema` 显式排除了
`expected_type == "integer" and isinstance(value, bool)`(`workflow_v2.py:562`),
否则 `True` 会被 `isinstance(True, int)` 误判成合法整数。

### 6. 三个 gate resolver

parking step 之后,外部事件触发以下任一 resolver 让 run 继续往前:

```
checkpoint   ─ resolve_gate(decision="approve"|"reject", approver, feedback)
webhook_wait ─ resolve_webhook_wait(payload)              # 隐式 approve
email_wait   ─ resolve_email_wait(email_dict)             # 隐式 approve
```

所有 resolver 共享同一个骨架:校验 step 类型 + 当前 `sr.status == "paused"` →
把 gate_output 写进 `run.state[step_id]` → `current_step_idx += 1` → 若已到
末步 `status="completed"`,否则保持 `paused` 等下一次 `drive_run`。

**approver 鉴权**(`workflow_v2.py:464`):若 step 配了
`config.approvers=["alice","bob"]`,调用方传的 `approver` 必须在列表里;空/
缺列表 = 任何持有 `workflow:approve` scope 的人(HTTP 层兜底)。

### 7. webhook_wait 的共享密钥

外部系统(GitHub/Stripe)没法带 tenant bearer,所以
`POST /runs/{rid}/webhook/{sid}` 支持两种鉴权之一(`server/routes/workflow_v2.py:401`):

- 标准 tenant bearer + scope(内部调用/测试);
- 或者 `?webhook_id=...` query 参数,必须等于 `step.config.webhook_id`
  —— 这是替代 tenant 边界的"共享密钥"。

`resolve_webhook_wait` 还支持 `config.event_filter`(`workflow_v2.py:619`):
一个 webhook URL 配多个 wait step,按 `payload["event"]` 分流。

---

## 操作与配置

### HTTP 路由(`mini_cc/server/routes/workflow_v2.py`)

| 方法 | 路径 | scope | 用途 |
|------|------|-------|------|
| GET    | `/tenants/{tid}/projects/{pid}/workflow-definitions` | `projects:read` | 列定义 |
| POST   | `/tenants/{tid}/projects/{pid}/workflow-definitions` | `projects:write` | 建定义 |
| GET    | `.../workflow-definitions/{def_id}` | `projects:read` | 取定义 |
| PUT    | `.../workflow-definitions/{def_id}` | `projects:write` | 改定义(version+1) |
| DELETE | `.../workflow-definitions/{def_id}` | `projects:write` | 删定义 |
| POST   | `.../workflow-definitions/{def_id}/runs` | `projects:write` | 起新 run |
| GET    | `/tenants/{tid}/projects/{pid}/workflow-runs` | `projects:read` | 列 run(可选 `?def_id=`) |
| GET    | `.../workflow-runs/{run_id}` | `projects:read` | 取 run |
| DELETE | `.../workflow-runs/{run_id}` | `projects:write` | 取消 run |
| POST   | `.../workflow-runs/{run_id}/drive` | `projects:write` | 同步驱动,body `{"session_id": "..."}` |
| POST   | `.../workflow-runs/{run_id}/steps/{sid}/resolve` | `projects:write` | 解 checkpoint gate |
| POST   | `.../workflow-runs/{run_id}/webhook/{sid}` | (webhook_id 或 bearer) | 解 webhook_wait |
| POST   | `.../workflow-runs/{run_id}/email/{sid}` | (无鉴权,filter 自兜底) | 解 email_wait |

### Step 配置 knob(放 `config` 字典)

| Step 类型 | config 字段 |
|-----------|-------------|
| `checkpoint` | `approvers: [str]`(可选) |
| `webhook_wait` | `webhook_id: str`(共享密钥)、`event_filter: str`(可选) |
| `email_wait` | `from_filter: str`、`subject_filter: str`(子串、大小写不敏感) |
| `validate` | `check: str \| {"schema": {...}}` |

### Run status 取值

`pending | running | paused | completed | failed | cancelled`
(`workflow_v2.py:166`)

---

## 验证步骤

后端跑在 `:8002`,先建 tenant / project / API key(scope 至少含 `projects:write`)。

```bash
BASE=http://localhost:8002
TENANT=t_demo; PROJ=p_demo
AUTH="Authorization: Bearer <YOUR_KEY>"

# 1) 建一个带 checkpoint 的定义
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-definitions" \
  -H "$AUTH" -H "Content-Type: application/json" -d '{
    "name": "approve-demo",
    "steps": [
      {"id":"draft","type":"action","prompt":"Draft a one-paragraph release note for {feature}."},
      {"id":"review","type":"checkpoint","config":{"approvers":["alice"]}},
      {"id":"publish","type":"action","prompt":"Publish the approved note. Context: {review}"}
    ],
    "state_schema": {}
  }' | jq '.def_id, .version'

# 2) 起 run
DEF=wfdef_xxx     # 上一步返回的 id
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-definitions/$DEF/runs" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"initial_state":{"feature":"streaming search"},"trigger":{"type":"manual"}}' \
  | jq '.run_id, .status, .current_step_idx'

# 3) 驱动(run 会在 checkpoint 处停下)
RUN=wfrun_xxx; SESS=sess_xxx
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/drive" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESS\"}" | jq '.status, .current_step_idx, .step_runs[1].status'
# 期望:status="paused", step_runs[1].status="paused"

# 4) 解 gate(必须是 alice)
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/steps/review/resolve" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"decision":"approve","approver":"alice","feedback":"lgtm"}' \
  | jq '.status, .current_step_idx'

# 5) 再 drive 一次跑完 publish
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/drive" \
  -H "$AUTH" -H "Content-Type: application/json" -d "{\"session_id\":\"$SESS\"}" \
  | jq '.status, .state.review, .state.publish'
```

UI 轮询:前端在 `paused` 后弹 Approve/Reject 按钮;调用 `resolve` 之后再调一次
`drive` 让 run 跑到下一个 park 点或 `completed`。

---

## 常见坑与调试

1. **`def_version` 标签变了但行为没变?** 你改了定义但没起新 run —— 老 run 走的是
   `def_snapshot`。要看 run 实际执行的 steps,读 `run.def_snapshot.steps`,别读
   live def。
2. **`{step_id}` 没被替换?** 占位符必须是合法 Python 标识符
   `[a-zA-Z_][a-zA-Z0-9_]*`,且 key 必须在 `run.state` 或保留的 `step_id`。
   prompt 里的 JSON `{}` 不会被吞(找不到就原样保留)。
3. **validate 总是 pass?** `check` 没配的话直接当 no-op pass
   (`workflow_v2.py:532`)。记得在 step.config 里写 `check`。
4. **webhook_wait 收到 401 `invalid or missing webhook_id`?** step 配了
   `config.webhook_id` 但调用方没传 query 参数,或值不匹配
   (`server/routes/workflow_v2.py:428`)。这是替代 tenant bearer 的共享密钥。
5. **email_wait 报 "email does not match step filters"?** `from_filter` /
   `subject_filter` 是子串 + 大小写不敏感匹配(`workflow_v2.py:692`),空 filter
   才放行任意邮件。
6. **approver 被 reject 但报 "not in step's approvers list"?** step 配了
   `approvers=["alice"]` 但你传的 `approver="bob"`(`workflow_v2.py:465`)。
   空列表才是"任何 workflow:approve scope 持有者"。

---

## 延伸阅读

- 源码:`mini_cc/workflow_v2.py`(全文 733 行)、`mini_cc/server/routes/workflow_v2.py`(HTTP surface)
- 旧版工作流(对照):`mini_cc/workflow.py` + `mini_cc/tools/workflow.py`
- 三层插件 / `.mini_cc/` 布局:下一章 [11 — MCP 客户端与三层插件](11-mcp-plugins.md)
- 配套的 `/workflow` 斜杠命令(操作的是旧版 Workflow,不是 V2):
  `mini_cc/commands/registry.py:873`
- e2e 覆盖矩阵:`mini_cc/tests/e2e/NOTES.md`(W1/W4/W6 specs)
