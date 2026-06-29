[ < [13](13-teams-scheduler.md) ] · [English version](../en/14-web-ui-observability-deploy.md)

# 14 — Web UI、可观测性与部署

> 进阶内部原理第 14 章(全系列最后一章)。前面十二章拆完了 SDK 核心:存储、沙箱、Agent Loop、工具、skills、MCP、Hooks、会话、HTTP 传输、Memory、SubAgents、团队调度。这一章讲外面那层壳:**React Web UI**、**端到端可观测性**(结构化日志、trace_id、metrics、span)、**部署形态**(进程启动、数据目录、后端层级、前后端同源)。读完你应当能独立把 mini_cc 拉起来,从 UI 一路追到 LLM 调用。

---

## 问题与动机

SDK 核心 (`core/`、`tools/`、`projects/`) 对 HTTP 与 UI 零感知——可以纯 SDK 嵌进自己的后端。但一旦要把多租户、可被浏览器驱动的 agent 框架交给真人用,就会冒出四个 SDK 不肯回答的问题:

1. **怎么把 SSE 流喂进浏览器?** `EventSource` 只支持 GET 且无法带 Bearer token;`/send` 是 POST + 流式响应,需要手写的 fetch + `ReadableStream` 客户端。
2. **怎么"看见"正在跑的工作流?** Workflow V2 的 run 不是 LLM 流,而是一串离散步骤 (action / validate / checkpoint / webhook_wait / email_wait),其中有些暂停等人审。UI 要让用户随时知道"卡在哪、下一步是什么、上一步输出了什么"。
3. **怎么追一个慢请求?** 一个 send 横穿 middleware → deps 鉴权 → SessionManager → AgentLoop → 工具 → SSE 桥,日志里各条记各条的拼不起来。
4. **怎么从"源码能跑"到"线上能跑"?** 一条命令拉后端、一条拉前端、env 切沙箱后端、`dist/` 静态资源被同一个 FastAPI 挂载——部署要尽可能接近"单进程全栈"。

`mini_cc/web/` 回答 1-2;`mini_cc/server/{logging_config,middleware,tracing,metrics}.py` 回答 3;`mini_cc/server/{cli,app,runtime_context}.py` 回答 4。

---

## 设计与实现

### A. Web UI:Hash 路由的 React 应用

前端是 Vite + React 19 + react-router-dom 7 + zustand 单页应用,源码在 `mini_cc/web/src/`。路由用 **HashRouter** 而非 BrowserRouter,因为生产部署里前端被 FastAPI 同源挂载 (`app.py:227-247` 的 SPA fallback),`#/projects/...` 锚点路由不需要服务端配合就能深链访问。

入口 `main.tsx:15-30` 定义全部八条路由:`/` (Login)、`/tenants`、`/projects`、`/projects/:pid` (Workspace)、`/projects/:pid/workflows` (WorkflowV2)、`/admin/login`、`/admin/keys`、`/admin/metrics`。`App.tsx:8-40` 跑两套独立的鉴权闸——**普通租户用户**由 `useAuth` store 管、**admin** 由 `useAdmin` store 管:两套 token、两套 session,`/admin/*` 走 admin 闸。未登录访问受保护路由一律 `<Navigate to="/" replace />`。

三个主页面:

- **Workspace** (`pages/Workspace.tsx:36`) —— 项目对话主界面,三 tab:`chat` (消息流 + slash 命令 + 权限提示)、`files` (FileTree + FilePreview)、`run` (RunTablePanel)。SSE 流式响应通过 `streamSend` (`lib/sse.ts:42`) 进入 zustand chat store。
- **WorkflowV2** (`pages/WorkflowV2.tsx:27`) —— 工作流可视化编辑与执行 (W6),下面细讲。
- **Admin** (`pages/AdminKeys.tsx` / `pages/AdminMetrics.tsx`) —— 跨租户 key 管理 + 指标仪表盘。AdminMetrics 每 5 秒拉一次快照 (`REFRESH_MS = 5000`, `AdminMetrics.tsx:7`),Sparkline 渲染 RPS、token 速率。

### B. Workflow V2:三栏可视化编辑器

`WorkflowV2.tsx:27` 的版心是固定三栏布局,组件树全在 `web/src/components/workflowV2/`:左栏 Definitions + Runs、中栏类聊天的执行时间线、右栏 step 检查器。四个组件:

| 文件 | 角色 |
|---|---|
| `LeftPane.tsx:45` | 左栏:definitions 列表 (单击选、✎ 编辑、＋ 新建) + 按当前 def 过滤的 runs,每条带 `STATUS_BADGE` 状态色 (`pending/running/paused/completed/failed/cancelled`, `LeftPane.tsx:27-34`) |
| `RunView.tsx:44` | 中栏:执行时间线。每步一行 `[idx] step_id (type)` + 状态字形 (`✓ ✗ ⏸ ▶ ⏭`),`previewOutput()` 截前 240 字 (`RunView.tsx:424`)。`paused` 的 checkpoint 步骤行内渲染 Approve/Reject 按钮 + 可选 feedback (`RunView.tsx:356-386`) |
| `StepInspector.tsx:24` | 右栏:被选中步骤的完整定义 + 运行态——prompt、condition、config、`inputs_schema`、`outputs_schema`,以及运行态 `started_at`/`completed_at`/`output` (JSON 美化)/`error` |
| `DefinitionEditor.tsx:1` | 滑出式模态:新建/编辑定义,步骤可加/删/排序,按 step type 切换 config 字段;edit 模式带 🗑 删除 (有 `window.confirm`) |

**轮询策略**:后端目前没给 workflow events 单开 SSE 通道——run 靠 POST `/drive` 同步推进,或被外部 webhook/email 异步解析 (`workflowV2Store.ts:1-9` 注释)。所以前端用 `startRunPolling()` (`workflowV2Store.ts:97-118`):被选中的 run 处于非终态时,每 **2 秒** 拉一次 `getWorkflowV2Run` 合并进 store。run 进入终态 (`completed/failed/cancelled`,`isTerminalStatus()` 于 `workflowV2Store.ts:120`) 即停止。这是"够用就好"的折中——将来若加 SSE,只需把 `startRunPolling` 换成订阅流,中栏 `RunView` 不变。

#### "Drive with no session" UX 修复与 amber banner

action step 派发时,后端把 prompt 注入某个 chat session 的 AgentLoop。如果项目里没 session,`drive()` 客户端报 `"no session available"` (`RunView.tsx:100-104`)。但更早一步:`RunView` 挂载时通过 `listSessionMetas()` (`RunView.tsx:61-71`) 探测,**没 session 就在中栏顶部渲染 amber 横幅** + 直通 chat tab 的按钮 (`RunView.tsx:199-214`):

```tsx
{sessionsLoaded && !sessionId && !isTerminalStatus(run.status) && (
  <div className="border-b border-border bg-amber-50 dark:bg-amber-900/20 ...">
    <span>action steps need a chat session to dispatch — none exist in this project yet</span>
    <a href={`#/projects/${pid}`}>open chat tab →</a>
  </div>
)}
```

这是典型的"把后端约束翻译成 UI 提示":不报错、不打断,只把用户引到能解决问题的页面。配套修复见 `e2e/NOTES.md` 的 `7aa4fab` 提交。

### C. SSE 消费:fetch + ReadableStream + 自动重连

聊天流客户端在 `lib/sse.ts`。**不用 `EventSource`**——它不支持 POST body、不能带 `Authorization` 头。`streamSend()` (`sse.ts:42`) 用 `fetch()` POST + `res.body.getReader()` 手解 `id: <seq>\ndata: <json>\n\n` 分帧,识别 `data: [DONE]` 哨兵结束流。

B8 **断线自动重连** (`sse.ts:60-184`):中途断流且此前至少收到过一个事件时,按指数退避 (1s → 2s → 4s,默认最多 3 次) 重试,重试请求带两样东西——`Last-Event-Id: <last_seq>` 头(告诉服务端从哪条重放),以及 body 里 `resume: true`(触发服务端的 **resume-only 路径**,跳过 lock + LLM 派发,只从 per-session 事件日志回放,`sse.ts:65-76`)。

`resume=true` 的语义是关键修复(见 `NOTES.md` `aaa5793`):早期实现里 reconnect 会触发**重复 LLM 派发**。加 `resume` 闸后,断线恢复变成纯"重放未送达事件",不重复烧 token。4xx (除 409) 视为确定性错误不重试;5xx 和 409 `project_busy` 视为瞬时错误重试 (`sse.ts:111`)。

### D. 可观测性:结构化日志 + trace_id + span + metrics

**结构化日志** (`logging_config.py`)。`configure_logging()` (`logging_config.py:165-192`) 在根 logger 挂单一 `StreamHandler`,默认 **JSON** (一行一对象:`ts` ISO8601 毫秒、`level`、`logger`、`msg`、`trace_id`、`tenant`、加 `extra={...}` 透传字段),`MINI_CC_LOG_FORMAT=text` 切人类可读,幂等。`JsonFormatter.format()` (`logging_config.py:123-145`) 显式排除 `LogRecord` 保留字段 (`_RESERVED`),剩下的 `extra` 全展开——路由里 `logger.info("send.dispatched", extra={"pid":..., "dur_ms":...})` 直达聚合系统。**RedactingFilter** (`logging_config.py:48-83`) 是 B9 安全过滤,装 handler 上,每条记录落地前用 `_REDACT_PATTERNS` (`logging_config.py:32-45`) 抹凭证:`mck_...` → `[REDACTED:mck_key]`、`Bearer xxx` → `Bearer [REDACTED]`、`sk-ant-...` → `[REDACTED:anthropic_key]`、`api_key=...` → `api_key=[REDACTED]`,同时改写 `record.msg`/`args`/`exc_text`。

**请求级 trace_id + tenant** (`middleware.py`)。`TraceIdMiddleware` (`middleware.py:25-43`) 是**最外层** middleware (`app.py:127`):每个请求接受客户端 `X-Trace-Id` 头或 `new_trace_id()` 生成一个,从 `path_params["tid"]` 拿租户 id,两个值都写进 contextvars (`_TRACE_CTX`/`_TENANT_CTX`,`logging_config.py:24-25`)。contextvars 的好处是**穿透**——同一线程/异步任务里任何 `logger.xxx()` 无需传参,`JsonFormatter` 自动从 `_TRACE_CTX.get()` 读 trace_id 加进 JSON。请求结束 (finally) 清空防泄漏 (`middleware.py:38-41`);响应头回写 `X-Trace-Id` (`middleware.py:42`) 供客户端对账。

**端到端追踪** (tenant → project → session → step)。一次 send 的可观测链路:

```
浏览器 POST /send
  → TraceIdMiddleware: set_trace_id(<hex>), set_tenant("demo")
  → deps.py: require_scope("sessions:write")
  → routes/sessions.py: SessionManager.send(pid, sid, input)
       ↓ logger.info("send.dispatched", extra={"pid": pid, "sid": sid})
       ↓ AgentLoop.run(input) → yield event
            ↓ tracing.log_span("tool.bash", tool="bash", cmd=...) 包装工具
  → SSE 桥 (sse.py) 序列化进 id: N / data: {...} 帧
  → 客户端 sse.ts 解析 → zustand chat store
```

追慢请求:聚合系统里 `trace_id=<hex>` 一过滤,从 `send.dispatched` 到 `span.end span=tool.bash dur_ms=...` 全部行就出来了。`tracing.py:85-132` 的 `log_span(name, **fields)` 在无 OTel 时纯打日志;设 `MINI_CC_OTEL_EXPORTER` 就同时开真 OTel span 导出 otlp/jaeger/console,SDK 缺失优雅降级 (`tracing.py:38-49`)。

**Metrics + run history**。`MetricsMiddleware` (`middleware.py:46-87`) 用原始 ASGI (非 `BaseHTTPMiddleware`) 实现,这样能从 `scope["route"]` 拿 FastAPI 路由模板 (`middleware.py:89-97`),给每个 `(method, route_template, status, tenant)` 计数。RED 三件套:`http_requests_total` (counter)、`http_request_duration_seconds` (histogram)、`http_in_flight_requests` (gauge)。导出 `GET /metrics` (Prometheus 文本) 与 `GET /metrics.json` (JSON 快照,`app.py:185-197`),都标注 "trusted-network only"。Run history 与 step 输出通过 W1-W6 REST API (`GET /workflow-definitions`、`/workflow-runs`、`/workflow-runs/{run_id}`) 暴露,`RunView` 渲染时间线 + 行内输出预览,StepInspector 美化 `output`/`error`/`started_at`/`completed_at`。这就是工作流可观测性——没有 SSE,但状态完整持久化 + 2 秒轮询。

### E. 部署:从源码到生产

**E1. 启动**。`python -m mini_cc.server` 等价 `serve` 子命令;前端开发态 `cd mini_cc/web && npm run dev` (vite,默认 5173)。`cli.main()` (`cli.py:367-386`) 没子命令时默认 `cmd_serve` (`cli.py:56-140`),依次配日志、建 `data_dir`、探 docker、建 `ServerRuntimeContext`、wire 各 manager、`build_app()` 后 `uvicorn.run`。`.env` 在 `main()` 顶部用 `python-dotenv` 自动加载 (`cli.py:372-379`),缺失降级为读 `os.environ`。

**E2. 环境变量**(部署相关):

| 变量 | 默认 | 出处 |
|---|---|---|
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | `cli.py:22-23` |
| `MINI_CC_HOST` / `MINI_CC_PORT` | `127.0.0.1` / `8000` | `cli.py:118-119` |
| `ANTHROPIC_API_KEY` / `LITELLM_API_KEY` | — | config |
| `MINI_CC_SANDBOX_BACKEND` | `auto` (`auto`/`opensandbox`/`docker`) | `runtime_context.py:64` |
| `MINI_CC_LOG_FORMAT` / `MINI_CC_LOG_LEVEL` | `json` / `INFO` | `cli.py:69-70` |
| `MINI_CC_CORS_ORIGINS` | `*` | `cli.py:111-112` |
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` / `MINI_CC_RATE_LIMIT_RPM` | `60` / — | `cli.py:36-38` |
| `MINI_CC_SHARE_SECRET` / `MINI_CC_WEBHOOK_SECRET` | dev-fallback (重启失效,启动大字警告) | `app.py:38-54` |
| `MINI_CC_OTEL_EXPORTER` | — (`otlp`/`jaeger`/`console`) | `tracing.py:34` |
| `MINI_CC_WEB_DIST` | 自动探测 | `app.py:210` |

启动时 `cmd_serve` 显式记 `starting server` 一行 (`cli.py:122-129`),带 `host/port/data_dir/docker_available/sandbox_backend`;LLM 凭证缺失则再加 warning "`/send` 会 401" (`cli.py:133-138`)。

**E3. 数据目录**。`<MINI_CC_DATA_DIR>/keys.json` 是鉴权基石 (TenantKeyRegistry,丢了所有租户的 key 都失效);`tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}` 是项目布局;`tenants/<tid>/.storage/` 是状态分离边界;`.mini_cc/` 是 SYSTEM 层插件目录 (`cli.py:88-90` 的 `ensure_tier_dir`)。详见 [01](01-overview.md) 与 [02](02-storage-projects-sessions.md)。

**E4. 后端层级** (opensandbox → docker → subprocess)。`ServerRuntimeContext._build_runtime()` (`runtime_context.py:57-81`) 按 `MINI_CC_SANDBOX_BACKEND` 决策:`opensandbox` 优先 OpenSandboxRuntime,env 没配或不可用记 warning 回落 docker (`runtime_context.py:70-73`);`docker` 跳过直接 DockerRuntime;`auto` (默认) 先试 opensandbox 再 docker,都没有返回 `None`。返回 `None` 时,`_sandbox_factory` (`runtime_context.py:111-130`) 对标记 `container` 的租户**自动降级**到 `SubprocessSandbox`,降级事件记进 `self.degrades` (`runtime_context.py:117-119`)。这个软降级是有意的——docker 不可用不应 brick 服务,只是失去容器隔离。三层后端 + `MountSpec` 见 [03](03-sandbox.md)。

**E5. 前后端同源**。生产不需要两个 origin。`build_app()` (`app.py:210-247`) 自动探测 `mini_cc/web/dist` (顺序:`MINI_CC_WEB_DIST` env → `<cwd>/mini_cc/web/dist` → `<package>/web/dist`),找到就 mount `/assets/*` 为静态文件,加一条 catch-all GET 兜底返回 `index.html`——这是 HashRouter 深链能直接加载的必要条件。部署三步:`npm run build` 产出 `dist/` → `python -m mini_cc.server` 自动挂 → CORS 收紧成明确白名单。

### F. E2E 测试

`mini_cc/web/e2e/` 下有 Playwright 套件,`NOTES.md` 维护完整覆盖矩阵 (21 个 spec 通过)。`globalSetup.ts` (`e2e/globalSetup.ts:20-92`) 自动 keygen 两个 e2e key(细分 scope 一个、`*` 给 admin 一个)并建 e2e 项目,写进 `process.env.E2E_API_KEY` / `E2E_ADMIN_KEY`,spec 通过 `helpers.ts` 读。覆盖:**W1 CRUD** (`workflow_v2_w1_api.spec.ts`,definition create→get→list→update→delete)、**W2 checkpoint** (`workflow_v2_advanced.spec.ts`、`workflow_v2_resolvers.spec.ts`,Approve 推进 / Reject 失败)、**W3 webhook_wait**、**W4 email_wait** (`workflow_v2_email.spec.ts`,匹配/不匹配/状态错误)、**W5 validate**、**W6 UI** (`workflow_v2*.spec.ts`,authoring/empty state/polling/def CRUD)、**R2 hardening** (`round2_hardening.spec.ts`,跨租户隔离)、**B8 SSE resume** (`round2_sse*.spec.ts`,`Last-Event-Id` 接受、`resume=true` 跳过派发只回放、客户端断线重连)。

`NOTES.md` 还诚实记录了这一轮发现并修复的 gap(空状态没"start run"按钮、DefinitionEditor 没 delete、SSE reconnect 重复派发),以及尚未覆盖项(action step 真实 LLM 派发、IMAP poll loop、需故障注入的 batch 测试)。

---

## 操作与验证

后端默认 `:8000`、前端默认 `:5173`(本机 `NOTES.md` 里实际是 `:8002` 与 `:5174`)。

```bash
# 1. 后端
MINI_CC_DATA_DIR=$PWD/mini_cc_data ANTHROPIC_API_KEY=sk-... python -m mini_cc.server
# 启动 JSON 行应带 host/port/data_dir/sandbox_backend;LLM 凭证缺失则有 warning "/send will fail with 401"

# 2. 前端(开发态,跨 origin)
cd mini_cc/web && npm install && npm run dev

# 3. trace_id 端到端:响应头回写 X-Trace-Id,后端日志 JSON 带 "trace_id"
curl -sS -D - http://127.0.0.1:8002/health -H "X-Trace-Id: my-trace-123" | head

# 4. RedactingFilter:跑一次 /send 后 grep stdout "mck_" → 只看到 [REDACTED:mck_key]

# 5. metrics 端点
curl -sS http://127.0.0.1:8002/metrics | head      # Prometheus 文本
curl -sS http://127.0.0.1:8002/metrics.json        # JSON 快照

# 6. 后端层级选择:启动日志的 sandbox_backend 字段是实际 runtime 类名
MINI_CC_SANDBOX_BACKEND=auto python -m mini_cc.server 2>&1 | head

# 7. 同源生产部署
cd mini_cc/web && npm run build && cd - && python -m mini_cc.server
# 浏览器开 http://127.0.0.1:8000/ → 直接渲染 React UI (来自挂载的 dist/)

# 8. 跑 e2e
cd mini_cc/web && E2E_API_KEY=mck_... npx playwright test --project=chromium
```

Workflow V2 手动验证:进 `#/projects/<pid>/workflows`,点 ＋ new definition 加几步(至少一个 checkpoint),保存后中栏空状态出现 `▶ start new run`;run 起来后 polling 每 2 秒拉;checkpoint 步骤显示 `⏸ paused` + 行内 Approve/Reject,点击触发 `/drive` 并通过 polling 看到推进。

---

## 常见陷阱与最佳实践

1. **`/health` ok 但 `/send` 401。** `ok` 只表示进程活着,真正的就绪信号是 `llm_configured` (`app.py:182-183`)。readiness probe 建在这个字段上。`cmd_serve` 凭证缺失会显式 warning,认真读启动日志。

2. **Workflow V2 run 不推进,还停在 paused。** 检查 (a) 有没有 chat session——RunView 的 amber banner 应当出现,没出现说明 `sessionsLoaded` 还没 resolve;(b) drive 是否真发了——`drive()` 在 `sessionId` 为空时直接报错 (`RunView.tsx:100-104`),不会默默失败。

3. **SSE 断线后客户端重复跑 LLM。** B8 修复之前会发生;现在确认:服务端收到 `body.resume=true` 走 resume-only 路径,客户端 `sse.ts:65-68` 只在 `attempt > 0` 时设 `resume=true` 与 `Last-Event-Id`。fork 客户端时别把 `resume` 默认设 true——初始请求必须是正常派发。

4. **JSON 日志看不懂。** 设 `MINI_CC_LOG_FORMAT=text` 切人类可读 (`logging_config.py:178-179`)。生产仍建议 JSON,字段稳定可机器解析。

5. **改了 `.env` 没生效。** `main()` 在解析 argv **之前** `load_dotenv()` (`cli.py:372-379`),但 dotenv 只在 cwd 或包目录有 `.env` 时生效。`.env` 在别处要么 `cd` 过去要么 `export`。

6. **`npm run build` 后 dist 没挂载。** 探测顺序 (`app.py:212-218`):`MINI_CC_WEB_DIST` → `<cwd>/mini_cc/web/dist` → `<package>/web/dist`。从仓库根启动且 `<repo>/mini_cc/web/dist/index.html` 存在就自动挂。

7. **日志里 `docker_available: false` 但 tenant 配了 container。** 这是软降级——`ServerRuntimeContext` 对每个 container tenant 回落 subprocess 并记 degrade (`runtime_context.py:117-119`)。看 `ctx.degrades` 或换 backend。

8. **CORS `*` 但带 cookie / Authorization 不工作。** `*` 与 `allow_credentials=true` 在浏览器规范里互斥。同源挂载后设 `MINI_CC_CORS_ORIGINS` 成明确白名单。

---

## 小结

mini_cc 的"壳"由三块拼起来:**React Web UI** 把 SDK 能力翻译成可点击的对话/工作流/管理界面,HashRouter + zustand + fetch/ReadableStream SSE 客户端是骨架;**可观测性**靠 `TraceIdMiddleware` 注 contextvars、`JsonFormatter` 出结构化行、`RedactingFilter` 脱敏、`MetricsMiddleware` 计 RED、`tracing.log_span` 选配 OTel——五者合力,让一个跨 middleware/SessionManager/AgentLoop/工具/SSE 桥的请求能用一个 `trace_id` 串到底;**部署**追求"单进程全栈":`python -m mini_cc.server` 一条命令拉后端,`npm run build` 后 `dist/` 被同一 FastAPI 挂同源,沙箱后端 opensandbox/docker/subprocess 三层软降级。这三块把能跑的 SDK 变成能上线、能运维、能给人用的产品。

---

### 全系列收尾

这是 mini_cc 进阶内部原理的第十四章,也是最后一章。十四章走完一圈:从 [01 总览](01-overview.md) 的分层架构与"无模块级全局状态"决策出发,我们拆过存储/项目/会话 ([02](02-storage-projects-sessions.md))、三层沙箱 ([03](03-sandbox.md))、Agent Loop 与工具派发 ([04](04-agent-loop.md))、Tools 工具箱 ([05](05-tools.md))、Permissions 权限模型 ([06](06-permissions.md))、HTTP Server ([07](07-http-server.md))、SSE 流与断线恢复 ([08](08-sse-streaming.md))、Auth 认证授权与租户隔离 ([09](09-auth.md))、Workflow V2 ([10](10-workflow-v2.md))、MCP 客户端与三层插件 ([11](11-mcp-plugins.md))、Skills/Slash/LSP ([12](12-skills-commands-lsp.md))、Agent 团队与三种异步原语 ([13](13-teams-scheduler.md)),最后到这里——Web UI、端到端可观测性、部署形态。覆盖了从最底层 Protocol+多实现的扩展点,到最外层面向真人的浏览器界面与运维手册的全部子系统。按顺序读下来,现在应当能解释 mini_cc 每条链路的"为什么这么设计"与"在哪能扩展";跳着读的话,回到 [01](01-overview.md) 总览图定位自己关心的子系统再深入。mini_cc 的价值不在于实现了多少功能,而在于把这些功能组合成一个分层清晰、可被嵌入、可观测、可扩展的框架——希望这套教程帮你建立了这份全景地图。
