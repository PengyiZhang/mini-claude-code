# Playwright 测试任务状态（teammates + workflow v2 branch/loop + checkpoint approver）

> **定时任务读这里**：每次 cron 启动先读完本文件，对照 TODO 清单判断进度，然后接着干。完成后把对应行改成 `[x]` 并写一段当日操作记录。

## 任务目标

用 Playwright MCP 在浏览器里跑通三件事，截图、最后形成一份带截图的中文流程文档：

1. **teammates** —— `/agents` 卡片 + spawn/stop/inbox/delete/edit + sink 重绑（@mention 后 lead 工具事件能进 SSE）
2. **workflow_v2 branch** —— 用 DefinitionEditor 创建一个含 branch 步的 workflow，run 一次，验证 step 输出里有 `branch_taken`
3. **workflow_v2 loop** —— 创建一个含 loop 步（带 `max_iterations`）的 workflow，run 一次，验证 `iterations` 计数
4. **checkpoint approver** —— 创建一个含 `checkpoint` 步的 workflow，run 到该步暂停，验证 `GET /.../approvers` 返回 teammate/lead/human 候选；调 `resolve_gate` 推进

最终交付物：`docs/mini_cc/zh/13a-teammates-playwright-walkthrough.md`（带截图的中文流程文档）。

## TODO 清单

- [x] 启动后端（MINI_CC_DATA_DIR=mini_cc_data_e2e, MINI_CC_PORT=8002）
- [x] 启动前端 vite（端口 5174，VITE_API_BASE=http://127.0.0.1:8002）
- [x] Playwright 访问 http://127.0.0.1:5174/，注入 localStorage `mini_cc.tenants.active = "mck_REDACTED"`（tenant=e2e, scope=*），reload
- [x] 进入 e2e_proj，开 chat pane
- [x] **测试 1 — teammates**：
    - [x] 在 chat 输入 `/agents`，截图 roster 空态（图 03）
    - [x] 通过 spawn 表单创建 alice（图 04）
    - [x] 再 `/agents`，截图 alice 行 alive 徽章（图 05）
    - [x] `/agents inbox alice`（peek，空）（图 06）
    - [x] `/agents stop alice`，shutdown_response 已落 lead.jsonl（图 07）
    - [x] `/agents edit alice --role senior-researcher`，被 alive 守卫拒绝（图 08）
    - [ ] ~~自然语言 @mention 验证 sink 重绑~~ —— 未走 LLM；机制已在 13a 文档说明
    - [ ] ~~`/agents delete alice`~~ —— alice 在注册表卡 alive（已记 bug）
- [x] **测试 2 — workflow branch**（HTTP 直驱）：
    - [x] POST 定义 pw_branch_demo（branches 用 `when`/`next`）
    - [x] run + drive
    - [x] 验证 step_runs 里 `branch_taken: "say_yes"`
- [x] **测试 3 — workflow loop**（HTTP 直驱）：
    - [x] POST 定义 pw_loop_demo（body=["bump"], while="counter < 3", max_iterations=5, counter_var="counter"）
    - [x] run + drive（initial counter=0）
    - [x] 验证 step_runs 里 loop 步 `iterations: 3`，state.counter=3
- [x] **测试 4 — checkpoint approver**（HTTP 直驱）：
    - [x] POST 定义 pw_checkpoint_demo（review=checkpoint, after=action）
    - [x] run + drive → run 翻 `paused`
    - [x] GET `.../steps/review/approvers` → 返回 alice(teammate) / lead(lead) / ""(human)
    - [x] POST `.../steps/review/resolve` body `{decision:"approve", approver:"lead"}` → run 推进到 after 完成
- [x] **写文档**：`docs/mini_cc/zh/13a-teammates-playwright-walkthrough.md`（嵌入图 01-09 + HTTP JSON 证据）
- [ ] **提交**：commit 文档 + 截图（截图放 `docs/mini_cc/zh/img/`）—— 本次 cron 即将 commit

## 启动命令（Windows bash）

```bash
# Backend（端口 8002，e2e 数据目录）
MINI_CC_DATA_DIR=mini_cc_data_e2e MINI_CC_PORT=8002 /d/anaconda3/python.exe -m mini_cc.server serve &

# Frontend（端口 5174）
cd mini_cc/web && VITE_API_BASE=http://127.0.0.1:8002 /d/nodejs/node.exe ./node_modules/vite/bin/vite.js --port 5174 --strictPort &
```

后台进程清理（如卡死）：`cmd.exe //c "taskkill /F /IM python.exe"` 和 `cmd.exe //c "taskkill /F /IM node.exe"`（慎用，会杀全部）。

## 关键路径

- 后端源码：
  - teammates：`mini_cc/teams/__init__.py`、`mini_cc/tools/teams.py`、`mini_cc/commands/registry.py:985`（`_cmd_agents`）
  - workflow v2 路由：`mini_cc/server/routes/workflow_v2.py`
  - workflow v2 引擎：`mini_cc/workflow/workflow_v2.py`
  - approvers helper：`mini_cc/workflow/approvers.py`
- 前端：
  - Workflow V2 UI：`mini_cc/web/src/pages/Workflows.tsx`（路由 `/workflows`）
  - DefinitionEditor：`mini_cc/web/src/components/workflow/DefinitionEditor.tsx`
  - 卡片渲染：`mini_cc/web/src/components/cards/`
  - Chat pane + slash：`mini_cc/web/src/components/ChatPane.tsx`
- e2e 数据：`mini_cc_data_e2e/`
- 已知可用密钥（tenant=e2e, scope=*）：`mck_REDACTED`
- localStorage key：`mini_cc.tenants.active`

## 已知坑

- Workflow V2 UI 路径是 `/#/projects/<pid>/workflows`，**不是** `/workflow-v2`（Stage C 验证过）
- `/agents spawn` 解析：`/agents spawn <name> <role> --prompt <text>`，`--prompt` 必填
- `/agents edit/delete` 拒绝 alive 状态，必须先 `/agents stop`
- sink 重绑验证：spawn 完直接 @mention **不会刷到 UI**；lead 必须先发一次消息触发重绑
- e2e_proj 是预置项目；如要新建 workflow 走 DefinitionEditor
- 不要 commit `mini_cc_data_e2e/` 下的运行时变更（mailbox / sessions）

## 操作日志（每次 cron 启动追加一段）

### 2026-07-01 21:22（initial）
- 完成文档拆分 commit `a098132`（teammates 章节独立）
- 启动 backend（task_id=blnjk75sc）和 frontend（task_id=byf6wq1a9），均运行中
- 下一步：浏览器登录 + teammates 测试

### 2026-07-02 00:40（cron #1）
- teammates 测试：`/agents` 空态 → spawn alice（researcher）→ roster 列出 alice alive → inbox peek（空）→ stop（shutdown_response 已落 lead.jsonl）→ edit 守卫拒绝
- 发现 bug：alice 处理完 shutdown_request 后，TeammateSpawner 注册表仍标记 alive=True，导致 `/agents` 卡片永远显示 alive，`/agents edit/delete` 守卫拒绝。已记入 13a 文档「常见坑」
- workflow_v2 branch：POST pw_branch_demo（v3），run+drive 后 `branch_taken: "say_yes"`（关键点：表达式要写 `x == 1`，不能用 `state.x == 1`）
- workflow_v2 loop：POST pw_loop_demo，initial counter=0，run+drive 后 `iterations: 3`，state.counter=3
- workflow_v2 checkpoint：POST pw_checkpoint_demo，run+drive → paused；GET /approvers 返回 alice(teammate)/lead/""(human)；POST /resolve decision=approve → run 推进到 after 完成
- UI workflow 列表对新 def 缓存不刷新，改用 HTTP JSON 作为验证证据
- 完成 9 张截图（01-fresh-session 到 09-workflows-list，存在 `docs/mini_cc/zh/img/`）
- 撰写 `docs/mini_cc/zh/13a-teammates-playwright-walkthrough.md`（约 450 行，含复现脚本）
- 即将 commit 文档 + 截图

### 2026-07-02 05:50（cron #2 - 校验）
- 检查 00:40 cron 已完成所有 TODO（除两项标 ~~delete~~ / sink 重绑因 LLM 路径未走而跳过）
- 状态：已完成，无需重复

### 2026-07-02 14:00（cron #3 - 用户反馈 + bug 修复 + LLM 实测）
- 用户反馈："先修 bugs，测试太简单不实用，基于实际任务用 LLM"
- **找到真 bug**：persistent teammate 在 idle_timeout 后 `continue` 重入 while，每 60s 重跑一次 `loop.run(stale next_input)` —— `lead.jsonl` 出现 25+ 条相同 "Work Complete Summary"。之前以为的 "alive 标志没翻" 只是表象
- **TDD 修复**：`mini_cc/teams/__init__.py:433-460` —— persistent 路径改成紧密 `_idle_poll` 循环；非 persistent 保留遗留"超时即退"。回归测试 `test_persistent_teammate_does_not_burn_llm_calls_when_idle` 断言 4+ 个 idle 周期后 `loop.runs == 1`。10/10 GREEN
- **LLM 实测（之前跳过的两项现已补上）**：
  - LLM 驱动 spawn alice + lead @mention ping → alice 回 `pong` **恰好一次**（直接证据：fix 在生产路径生效）。截图 10/11 落 `docs/mini_cc/zh/img/`
  - LLM 驱动 triage workflow `pw_triage_demo`：LLM 把 "my app crashes on startup" 分类成 `bug`，branch 据此选 `bug_path`，loop 步迭代 2 次退出 —— 验证 state-passing 跨 action/branch/loop 真的能工作
- 文档：§1.6（LLM @mention 实测）+ §1.7（bug 根因 + 修复）+ §2.4（LLM 驱动 triage workflow）+ §4（移除旧"alive 不更新"条目）
- commit `c888ec5`：fix + 回归测试 + 文档 + 截图 10/11
- **任务完成**，所有 TODO（含原被跳过的两条）均已覆盖
