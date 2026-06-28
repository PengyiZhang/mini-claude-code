# 生产级功能审查 — Round 2(四路 Explore Audit 合并)

> **For Claude:** 本文档是 Round 2 audit 结论的固化 + 分批执行计划。
> 与 `2026-06-28-p0-remediation.md` 的关系:那篇是 Round 1 三路 audit,
> 本篇覆盖 Round 1 之后新发现的问题,以及 Round 1 修复后浮现的次级问题。

**生成时间:** 2026-06-28
**当前分支:** `dev-functional`(873 tests passing)
**审计来源:** 4 个 explore agent 并行执行,各盯一个子系统
  - 多 agent 子系统(subagent / teammates / workflows / skills / slash commands)
  - 存储与状态子系统(FSStorage / 调度器 / MCP / webhook / tenant 隔离)
  - (合并阶段)二次核实 agent 标记的 P0,剔除误报

**核实原则:** 凡 agent 标 P0 的项,在合并前都通过 Read 工具实地核查源码;
误报(如 "workflow state bag 不持久化"——实际 `_persist_active` 已调用)
已剔除。

---

## 0. 优先级矩阵(去重 + 校准后)

按"风险严重度 × 触发频率 × 修复成本"排序。

### P0 — 生产阻塞(数据丢失 / 资源泄漏 / 崩溃)

| ID | 问题 | 文件:行 | 根因 |
|----|------|---------|------|
| **A1** | `save_messages` / `save_todos` / `save_task` 非原子写 | `storage/fs.py:74-78, 263-265, 278-280` | 直接 `fp.write_text()`,kill -9 中途 → JSON 损坏 |
| **A2** | `load_messages` 损坏时静默返回 `[]` | `storage/fs.py:71-72` | JSONDecodeError 直接吞掉,用户看到"空对话"无法区分"没消息"还是"坏了" |
| **A3** | `MCPPool.disconnect` 不调 `client.close()` | `mcp/client.py:248-249` | `_clients.pop(name, None)` 扔引用,stdio 子进程 / HTTP 连接池全泄漏 |
| **A4** | `BackgroundScheduler.stop()` 不杀子进程 | `tools/background.py:148-172` | 只标记 `task.status = "stopped"`,底层 bash 子进程继续跑 |
| **A5** | SSE `asyncio.Queue()` 无 maxsize | `server/sse.py:35` | 慢客户端 + 高频事件 → 服务端无界缓冲 → OOM |
| **A6** | SSE 无心跳 | `server/sse.py:20-77` | 长工具调用期间无输出,nginx / cloudflare 60s 超时断连 |

### P1 — 功能性缺陷

| ID | 问题 | 文件:行 | 修复方向 |
|----|------|---------|---------|
| **B1** | 串行工具调用 | `core/loop.py:784` `_execute_tool_calls` | Anthropic 支持单 response 多 tool_use 并行;mini_cc 一个一个跑。`ThreadPoolExecutor` 并发 dispatch,保序聚合 |
| **B2** | `loop.run()` 无并发保护 | `core/loop.py` | SDK 直接用法可两次进 `run()`,消息数组竞态。加 `self._running` Lock |
| **B3** | Hook 抛异常 → 整轮挂 | `core/hooks.py:46-53` | 没 try/except 包,异常冒泡整个 turn。包装,降级成 tool_result 错误 |
| **B4** | Teammate 任务认领竞态 | `teams/__init__.py:395-406` | `_claim_task` 读-改-写无 CAS;两个 idle teammate 可同时认领同一 task。storage 层加文件锁 |
| **B5** | Plan approval 死等 | `teams/__init__.py:294-298` | lead 中途死了 teammate 永远 poll。加超时(默认 10 min)+ lead 健康度检查 |
| **B6** | TeammateSpawner 无优雅关闭 | `teams/__init__.py:192` | daemon 线程服务退出时被强杀,mailbox 半写入消息丢失。加 `shutdown(timeout=...)`,wire 到 FastAPI lifespan |
| **B7** | Rate limit 非分布式 | `server/ratelimit.py:41-78` | 内存计数;多进程部署每进程独立额度。生产需要 Redis 后端 |
| **B8** | SSE 无重连支持 | `server/routes/sessions.py:157-201` | 不处理 `Last-Event-Id`,客户端断网丢事件。需 event id 持久化 + replay |
| **B9** | 无日志脱敏 | `server/logging_config.py:45-84` | API key / bearer token 可能进日志。加 redact filter |
| **B10** | Subagent MCP 工具被默默排除 | `core/subagent.py:23-25` | `SUBAGENT_TOOL_NAMES` 硬编码白名单,MCP 全 silent drop。加 `allow_mcp: bool` 参数 |
| **B11** | Workflow 并发执行未锁 | `tools/workflow.py:121-170` | 同 wf 两次 `run_all` 乱序。per-wf reentrant lock |
| **B12** | Cron 错过不补跑 | `scheduler/cron.py:163-169` | 重启后 past fire-time job 永远跳过。记 `last_tick`,补跑窗口内 job |

### P2 — 粗糙之处(可选)

- 工具 cancel 事件缺 `partial` 标志(`core/loop.py:530`)
- 除 `/cost` 外其它 slash command 没 scope 元数据(`commands/registry.py`)
- Skill 名字冲突跨 tier 默默覆盖,无 warning(`plugins/discover.py`)
- Skill 无 hot-reload(`skills/loader.py`),需要 `/skills` 手动 scan
- Tenant inbox 文件无大小上限(`teams/__init__.py:66-73`)
- `worktree.py:39-46` events.jsonl 非原子追加
- Webhook 无去重 / 幂等 key(`sharing/webhooks.py`)
- 健康检查只查 LLM 凭证,不查 storage / disk / dependencies(`server/app.py:149-157`)
- 静态资源无 cache headers(`server/app.py:206-209`)
- Tracing 用 `X-Trace-Id` 而非 W3C `traceparent`
- `_safe_session` 只过滤特殊字符,不限长度(`storage/fs.py:61-62`)

### ❌ 已剔除的误报(agent 标 P0 但实际无问题)

| 误报 | 核实结论 |
|------|---------|
| Workflow state bag 不持久化 | 实际 `workflow_set_state` 末尾调 `_persist_active(ctx, wf)`(`tools/workflow.py:216`),已落盘 |
| Share token 无 replay 保护 | HMAC + nonce + TTL 是标准做法,one-time-use denylist 是过度防御 |
| Auth token 轮换无 overlap | `grace_hours` 已扩展旧 key 的 expiry |

---

## 1. 分批执行计划

按"主题相关 + 修复路径相似"分批,每批可独立合并。

### Batch 3 — 原子性 + 资源泄漏(A1, A2, A3, A4)

**主题:** 修复"kill -9 / 重启 / stop 按钮"触发的数据丢失与资源泄漏。

| Task | 改动 | 验收 |
|------|------|------|
| 3.1 A1 | `save_messages` / `save_todos` / `save_task` 改用 `_atomic_write_json`(已有工具方法) | 单测:写过程中 kill 模拟,文件不损坏 |
| 3.2 A2 | 区分"不存在"和"损坏":后者抛 `StorageCorruptionError`,加日志 | 单测:损坏文件触发异常,不返回 `[]` |
| 3.3 A3 | `MCPPool.disconnect` 在 pop 前调 `client.close()` | 单测:disconnect 后 stdio 子进程已终止 |
| 3.4 A4 | `_BGTask` 加 `proc: subprocess.Popen` 字段;`stop()` 调 `proc.terminate()` + 超时 `kill()` | 单测:长时间 sleep 的 bash 被 stop 后立即退出 |

**预计:** 4 个 task,1 个 session。

### Batch 4 — SSE 健壮性(A5, A6, B8)

**主题:** 一次性补齐 SSE 三件套(限流 / 心跳 / 重连)。

| Task | 改动 | 验收 |
|------|------|------|
| 4.1 A5 | `asyncio.Queue(maxsize=512)`;满时关连接(等同客户端掉线) | 单测:慢消费者不会让 queue 无界增长 |
| 4.2 A6 | `await asyncio.wait_for(queue.get(), timeout=15)`,超时 yield `: keepalive\n\n` | 集成测:60s 工具调用期间连接保持 |
| 4.3 B8 | 每事件带 `id` 字段;route 处理 `Last-Event-Id` 头,从 storage 重放 | 集成测:断网重连后看到漏掉的事件 |

**预计:** 3 个 task,1 个 session。可能涉及前端 SSE 客户端改动。

### Batch 5 — 并发与生命周期(B1, B2, B4, B6)

**主题:** 并发护栏 + 资源优雅关闭。

| Task | 改动 | 验收 |
|------|------|------|
| 5.1 B1 | `_execute_tool_calls` 用 `ThreadPoolExecutor` 并发执行工具,聚合保序 | 单测:N 个 sleep(1) 工具总耗时 ≈ 1s 而非 Ns |
| 5.2 B2 | `AgentLoop` 加 `self._running = threading.RLock()`;`run()` 进入时获取 | 单测:并发两次 `run()` 第二次抛 `LoopAlreadyRunning` |
| 5.3 B4 | `_claim_task` 在 storage 加文件锁(CAS) | 单测:两个 teammate 同时 idle-poll,只有一个认领成功 |
| 5.4 B6 | `TeammateSpawner.shutdown(timeout=5)`;wire 到 FastAPI lifespan 的 shutdown 事件 | 单测:shutdown 后所有 teammate 线程已 join,mailbox 完整 |

**预计:** 4 个 task,1-2 个 session。B1 风险较高(并发改造)。

### Batch 6 — 错误反馈 + 可观测性(B3, B9, B7)

**主题:** 容错与日志合规。

| Task | 改动 | 验收 |
|------|------|------|
| 6.1 B3 | `Hooks.dispatch` 用 try/except 包每个 hook,异常降级成 `tool_result` 错误 | 单测:抛异常的 hook 不影响整轮 |
| 6.2 B9 | logging filter redacts `mck_*` / `Bearer ...` / `sk-...` | 单测:日志输出无明文凭证 |
| 6.3 B7 | `TenantRateLimiter` 加 Redis 后端(env 切换;无 Redis 时回退到内存) | 单测:多进程模拟,合并计数 |

**预计:** 3 个 task,1 个 session。B7 可拆出独立 PR(Redis 依赖)。

### Batch 7 — 功能完整性(B5, B10, B11, B12)

**主题:** 填补功能性缺口。

| Task | 改动 | 验收 |
|------|------|------|
| 7.1 B5 | `_wait_for_plan_verdict` 加超时(10 min,可配)+ 检查 lead session 是否还活着 | 单测:超时后 teammate 退出而非死等 |
| 7.2 B10 | `spawn_subagent` 加 `allow_mcp: bool = False` 参数;True 时把 `mcp_pool.all_tools()` 加进工具集 | 单测:allow_mcp=True 时 subagent 看到 `mcp__<server>__<tool>` 工具 |
| 7.3 B11 | `Workflow` 加 per-id reentrant lock;`run_all` 进入时获取 | 单测:并发两次 `run_all` 第二次阻塞 / 返回 already-running |
| 7.4 B12 | CronScheduler 记录 `last_tick_time`,启动时补跑窗口内的 missed fires | 单测:停机 5 分钟、每分钟一 fire 的 job,启动后补 5 次 |

**预计:** 4 个 task,1 个 session。

### P2 — 顺手改

不单独立批,做相关 batch 时顺手清,或独立小 PR。

---

## 2. 执行节奏

- **每批独立 commit + push gitee**,可独立合并到 main
- **每个 task 配单测**(FSStorage / MCP / hook 等都能纯 Python 测;SSE 重连需 httpx + asyncio)
- **每批结束跑全量回归**(目标:900+ tests,目前 873)
- **完成顺序建议:** Batch 3 → 4 → 5 → 6 → 7(从最高 P0 到 P1,P2 散落其间)
- **可跳过项:** B7(Redis 后端)如果当前部署单进程,可延后

---

## 3. 不在本轮范围

明确不在本次 audit 范围、但未来可能需要的工作:

- **Phase G/H**:Webhook 高级特性、Job Queue 系统化(已有 phase 设计文档)
- **OpenSandbox / Docker tier 的等价修复**:本轮只覆盖默认 FS + subprocess 后端
- **多语言 SDK**(目前只 Python + HTTP)
- **审计日志合规**(SOC2 / GDPR 维度,超出"功能审查"范围)
- **性能基准 / load test**(功能正确性 ≠ 性能达标,需独立项目)

---

## 4. 大型功能升级:动态 Workflow 模块

**用户反馈汇总(2026-06-28):** 现有 workflow 太简单,只是"换 prompt 的顺序流"。
缺:检查点 / 人工 gate、step 间 schema 校验、webhook/email 触发器、Definition vs Run
分离、专属 UI 页面、实时执行视图。

这是 Phase 级升级,单独设计文档落地:

👉 **`docs/plans/2026-06-28-workflow-upgrade-design.md`**

要点:
- Definition / Run 概念分离 + 持久化(依赖 A1 原子写)
- 步骤类型扩展:`action` / `validate` / `checkpoint` / `webhook_wait` / `email_wait`
- `WorkflowRunner` async 引擎 + 长生命周期 run(parked 不烧 CPU)
- 触发器:webhook 入站(复用现有 `WebhookRegistry`)、email(新模块 W4)、schedule
- 前端 Tab 重命名 `Run → Workflow`,三栏布局(导航 / chat-like 执行视图 / 步骤详情)
- 分 6 期落地(W1-W6),每期独立可合并

**与 Round 2 audit 的耦合:**
- W1 起步依赖 A1(原子写)—— 必须先做 Batch 3
- W2 / W3 涉及 SSE,建议 Batch 4(A5+A6 心跳 + 限流)先落地
- 不算入 Batch 3-7 序列,作为并行的 Phase 升级单独推进
