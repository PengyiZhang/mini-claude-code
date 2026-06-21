# mini_cc — 可集成后端的 Agent 框架

多租户、沙箱化、可被后端集成的 mini Claude Code。参考
`s20_comprehensive/` 中 19 个子系统的 harness 模式重新组织,面向
生产级多租户场景:按项目隔离、可插拔存储、无模块级全局状态、
可选的 HTTP/SSE 传输层。

模型是 driver,本包是 vehicle。既可以作为 SDK 嵌入 Python 后端,
也可以直接拉起 server,被任意语言的客户端驱动。

```
Agency 来自模型。mini_cc 给模型提供手、眼、工作区、队友,以及网络接口。
```

---

## 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  server/ (FastAPI、HTTP/SSE、按租户 API-key 鉴权)               │  ← 传输层
├─────────────────────────────────────────────────────────────────┤
│  session/  SessionManager(按项目加锁)                          │  ← 编排层
│  projects/ ProjectManager(租户元数据、目录布局)
├─────────────────────────────────────────────────────────────────┤
│  core/     AgentLoop、hooks、recovery、compaction、subagent     │  ← Agent 核心
│  teams/    MessageBus、ProtocolTracker、TeammateSpawner
│  tools/    bash、fs、todo、cron、task、worktree、mcp、...
│  skills/   按需 skill 加载
│  mcp/      MCP client / pool
│  scheduler/ CronScheduler
├─────────────────────────────────────────────────────────────────┤
│  sandbox/  SubprocessSandbox + Policy                            │  ← 隔离层
│  storage/  可插拔 Storage 接口,默认 FSStorage
│  auth/     TenantKeyRegistry(与传输无关)
└─────────────────────────────────────────────────────────────────┘
```

每一层只依赖它下面的层。SDK 核心对 HTTP 传输层无感知;Storage 和
sandbox 不依赖任何上层。**无模块级全局状态**,每个子系统实例都
是 per-project。

---

## 包布局

```
mini_cc/
  __init__.py            对外 API 表面
  config.py              AnthropicConfig(环境变量驱动)
  _shim.py               向后兼容 agent_loop(),给 s20 读者用

  storage/               Storage Protocol + FSStorage
  sandbox/               Sandbox 基类 + SubprocessSandbox + Policy
  auth/                  TenantKeyRegistry(JSON 存储,线程安全)

  core/
    loop.py              AgentLoop + ProjectRef
    system_prompt.py     runtime 分段式 prompt 组装
    compaction.py        分层 context 压缩 + transcript 快照
    recovery.py          重试 / token 升级 / 模型降级
    hooks.py             per-project Hooks 注册表 + 4 个 hook 工厂
    subagent.py          spawn_subagent(聚焦型单用途 loop)

  tools/                 每个 tool family 一个文件:
    bash.py  fs.py  todo.py  cron.py  skills.py
    task.py  worktree.py  background.py  mcp.py  teams.py  subagent.py
    base.py              ToolContext / FunctionTool / Tool 协议
    __init__.py          builtin_tools() 注册表

  skills/                SkillLoader + 按需 load_skill tool 后端
  mcp/                   MCPClient + per-project MCPPool
  scheduler/             per-project CronScheduler
  teams/                 MessageBus + ProtocolTracker + TeammateSpawner

  projects/              ProjectManager + ProjectMeta + 磁盘布局
  session/               SessionManager(per-project RLock 串行化)

  server/                P3 HTTP/SSE 传输层
    app.py               build_app() —— FastAPI 工厂 + lifespan + CORS
    sse.py               同步 Iterator → 异步 SSE 桥接
    deps.py              require_tenant / validate_id 依赖
    errors.py            MiniCCError 异常层级 + envelope 映射
    schemas.py           pydantic 请求/响应模型
    routes/
      projects.py        /tenants/{tid}/projects 下的 CRUD
      sessions.py        start / list / remove / send(SSE)
    cli.py               python -m mini_cc.server  [serve|keygen|keys|revoke]
    __main__.py          入口
```

---

## 公共 API 快速开始

### 作为 Python SDK(进程内调用)

```python
from mini_cc import ProjectManager, SessionManager

pm = ProjectManager("/data/projects")
pm.create(tenant_id="t1", project_id="demo")
sm = SessionManager(pm)
sess = sm.start_session("demo")

for ev in sm.send("demo", sess.session_id, "list the files here"):
    if ev["type"] == "text":
        print(ev["text"])
    elif ev["type"] == "tool_use":
        print(f"→ {ev['name']}({ev['input']})")
    elif ev["type"] == "tool_result":
        print(f"← {ev['content'][:80]}")
    elif ev["type"] == "done":
        break
```

`SessionManager.send()` 产出的事件类型:

| type                      | payload                                          |
| ------------------------- | ------------------------------------------------ |
| `text`                    | `{text: str}`                                    |
| `tool_use`                | `{name, input, id}`                              |
| `tool_result`             | `{tool_use_id, content}`                         |
| `done`                    | —                                                |
| `error`                   | `{message}`                                      |
| `max_tokens_escalation`   | `{max_tokens}`                                   |
| `cron_fired`              | `{job_id, prompt}`                               |
| `background_notification` | —                                                |

### 走 HTTP/SSE(任意语言)

```bash
# 给租户签发 key
python -m mini_cc.server keygen tenant1     # → mck_<32hex>

# 起服务
MINI_CC_DATA_DIR=/var/lib/mini_cc \
MINI_CC_HOST=0.0.0.0 MINI_CC_PORT=8000 \
python -m mini_cc.server
```

```bash
# 建项目
curl -X POST http://localhost:8000/tenants/tenant1/projects \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"project_id":"demo","display_name":"Demo"}'

# 开会话
curl -X POST http://localhost:8000/tenants/tenant1/projects/demo/sessions \
     -H "Authorization: Bearer mck_<key>" \
     -d '{"session_id":"s1"}'

# 发一轮 —— SSE 流
curl -N -X POST \
     http://localhost:8000/tenants/tenant1/projects/demo/sessions/s1/send \
     -H "Authorization: Bearer mck_<key>" \
     -H "Content-Type: application/json" \
     -d '{"user_input":"hello"}'
```

SSE 在线协议格式:

```
data: {"type":"text","text":"hi there"}\n\n
data: {"type":"tool_use","name":"bash","input":{"command":"ls"},"id":"tu1"}\n\n
data: {"type":"tool_result","tool_use_id":"tu1","content":"file1\nfile2"}\n\n
data: {"type":"done"}\n\n
data: [DONE]\n\n
```

---

## HTTP 端点

所有路由都在 `/tenants/{tid}/...` 下 —— URL 里的 `{tid}` **必须**
和 Bearer API key 反解出来的 tenant_id 一致,否则 403。

| Method   | Path                                              | 行为                                       |
| -------- | ------------------------------------------------- | ------------------------------------------ |
| `GET`    | `/health`                                         | 无鉴权的存活探针                           |
| `POST`   | `/tenants/{tid}/projects`                         | 创建;已存在 409;非法 id 400               |
| `GET`    | `/tenants/{tid}/projects`                         | 列出该租户的所有项目                       |
| `GET`    | `/tenants/{tid}/projects/{pid}`                   | 查单个;缺失或跨租户 404                   |
| `DELETE` | `/tenants/{tid}/projects/{pid}`                   | 删除;缺失 404                             |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions`          | 开会话;新建 201 / 幂等续接 200             |
| `GET`    | `/tenants/{tid}/projects/{pid}/sessions`          | 列出 SessionMeta(含 in_memory 标记)       |
| `DELETE` | `/tenants/{tid}/projects/{pid}/sessions/{sid}`    | 停止并注销;未知 404                       |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/resume` | 显式 warm 一个冷会话;幂等;不在盘上 404 |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/send` | 以 SSE 流式返回事件;自动 warm 冷会话;项目被占用时 409 |
| `GET`    | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions` | 列出待处理的权限请求;无 `permissions.toml` 时返回 `[]` |
| `POST`   | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions/{req_id}/decide` | body `{decision:"allow"\|"deny", message?}`;204 / 404 / 409 |
| `GET`    | `/tenants/{tid}/projects/{pid}/files/tree?path=`  | 列出目录子项;路径穿越 400                  |
| `GET`    | `/tenants/{tid}/projects/{pid}/files/content?path=` | 读取最多 256 KB 的文本文件                |
| `POST`   | `/tenants/{tid}/projects/{pid}/files/mkdir?path=` | 创建目录                                   |
| `POST`   | `/tenants/{tid}/projects/{pid}/files/upload?path=` | multipart 上传;可选 `rel_paths` 字段保留文件夹结构 |
| `DELETE` | `/tenants/{tid}/projects/{pid}/files?path=`       | 删除文件或目录                             |
| `GET`    | `/tenants/{tid}/projects/{pid}/download`          | 以 ZIP 流式下载项目工作区                  |

错误信封(所有错误统一一种形状):

```json
{
  "error": {
    "code": "not_found",
    "message": "project demo not found",
    "details": {}
  }
}
```

状态码映射:`KeyError → 404`、`ValueError("already exists") → 409`、
`ValueError(...) → 400`、未知异常 → 500。项目被占用的 409 会带
`details.code = "project_busy"`。

---

## 安全模型

**Sandbox(per-project):** `SubprocessSandbox` 强制:
- 路径校验 —— 所有 read/write/edit/glob/grep 路径解析到
  `project_root` 下;逃逸(含 `..` 和绝对路径)直接拒绝。
- 命令策略 —— bash 命令过 `Policy` 扫描;deny-list 命中抛
  `CommandBlockedError`。
- 环境变量过滤 —— 只透传 `Policy.allowed_env` 中的 key;`HOME` 和
  `USERPROFILE` 被改写为项目工作区。

**ID 校验:** project_id 和 session_id 必须匹配
`[A-Za-z0-9_-]+`(SDK 层和 HTTP 层双重校验)。这关掉了
`ProjectManager.delete` 的路径穿越漏洞。

**租户隔离:** 每个读/写路由都会校验资源的 `tenant_id` 是否和 URL 中的
`{tid}` 一致(后者通过 API key 鉴权)。跨租户访问返回 **404(而非
403)**,这样不会泄漏其他租户项目是否存在。

**API key:** 每个租户一把或多把 key,存到 `<data_dir>/keys.json`,
写入采用 rename-on-write 原子操作。文件结构为 JSON 映射
`key → {tenant_id, scopes, created_at, expires_at, label,
rotated_from}`。每把 key 三个正交维度:

- **Scopes** —— 最小权限。详见下面「鉴权(Phase D)」。
- **Expiry** —— 短时 key(CI、分享链接等)。
- **Rotation** —— 优雅轮换被泄漏的 key,可选 `--grace-hours`。

生成 / 列举 / 轮换:

```bash
python -m mini_cc.server keygen <tenant> [--scopes ...] [--expires-in 7d] [--label ...]
python -m mini_cc.server keys   list <tenant>
python -m mini_cc.server keys   rotate <key> [--grace-hours N] [--scopes ...] [--label ...]
python -m mini_cc.server revoke <key>
```

**鉴权(Phase D):** 每个路由通过 `Depends(require_scope("<resource>:<verb>"))`
声明所需 scope。held scope 不满足时返回 403,`details.code = "insufficient_scope"`,
响应头附 `WWW-Authenticate: Bearer scope="..."` 提示。Scope 语法:

| Scope          | 允许                                       |
| -------------- | ------------------------------------------ |
| `*`            | 任意操作(迁移过来的老 key 默认值)         |
| `read:*`       | 任意 GET                                   |
| `write:*`      | 任意非 GET                                 |
| `sessions:*`   | `/sessions/*` 上的任意方法                 |
| `sessions:read`| `/sessions/*` 上的 GET                     |
| `sessions:write` | `/sessions/*` 上的 POST/DELETE           |
| `files:read`   | `/files/*` 上的 GET(tree/content/download)|
| `files:write`  | `/files/*` 上的 POST/DELETE                |

`read` ≡ GET,`write` ≡ 其他。HTTP 层固定映射,路由层无需显式声明 verb。

**迁移:** Phase D 之前的 `keys.json`(裸字符串值)在首次读取时
自动升级为 `KeyRecord`,带 `scopes=["*"]`、`expires_at=None`、
`label="migrated"`。在下次 mutation 之前,文件不会被重写,所以老
fixture 字节保持不变。

**软沙箱,非容器化:** sandbox 是 defense-in-depth 一层,不是硬安全
边界。对不受信代码,请把 mini_cc 跑在容器或 VM 内(P5 工作,尚未交付)。

---

## 配置

环境变量(由 `python -m mini_cc.server` 读取):

| 变量名                  | 默认值             | 用途                                            |
| ----------------------- | ------------------ | ----------------------------------------------- |
| `ANTHROPIC_API_KEY`     | —                  | Anthropic API key(SDK 直接用)                 |
| `ANTHROPIC_BASE_URL`    | —                  | 备用 base URL(代理、Anthropic 兼容服务)       |
| `MODEL_ID`              | `claude-sonnet-4-6`| 主模型                                          |
| `FALLBACK_MODEL_ID`     | —                  | 失败时的降级模型                                |
| `MINI_CC_DATA_DIR`      | `./mini_cc_data`   | 项目、状态、key registry 的根目录               |
| `MINI_CC_HOST`          | `127.0.0.1`        | 服务绑定地址                                    |
| `MINI_CC_PORT`          | `8000`             | 服务端口                                        |
| `MINI_CC_CORS_ORIGINS`  | (空)              | 逗号分隔的允许跨域来源(给浏览器 SSE 用)       |
| `MINI_CC_METRICS_ENABLED` | `1`              | MetricsMiddleware 的总开关                     |
| `MINI_CC_OTEL_EXPORTER` | (空)              | `otlp` / `jaeger` / `console`(否则仅日志)     |
| `MINI_CC_OTEL_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点                          |
| `MINI_CC_OTEL_SERVICE_NAME` | `mini-cc`       | OTel resource 属性                             |

编程式配置:

```python
from mini_cc import AnthropicConfig, set_default_config
set_default_config(AnthropicConfig(
    api_key="sk-ant-...",
    primary_model="claude-opus-4-7",
))
```

---

## 可观测性(Phase E)

mini_cc 自带两个 metrics 端点 + 可选 trace 层。都继承「可信网络」
模型(默认绑定 127.0.0.1,`/metrics` 无 auth)。

**Metrics 端点**:

- `GET /metrics` —— Prometheus 0.0.4 文本格式。让 Prometheus 直接抓。
- `GET /metrics.json` —— 同一份数据的 JSON 快照,Web UI / 脚本好消费。

**Metrics 目录**:

| Metric | 类型 | 标签 | 来源 |
| ------ | ---- | ---- | ---- |
| `http_requests_total` | counter | method, route_template, status, tenant | MetricsMiddleware |
| `http_request_duration_seconds` | histogram | method, route_template, tenant | MetricsMiddleware |
| `http_in_flight_requests` | gauge | — | MetricsMiddleware |
| `anthropic_tokens_total` | counter | tenant, kind (input/output/cache_read/cache_create) | AgentLoop |
| `anthropic_request_total` | counter | tenant, status (success/error/cancelled) | AgentLoop |
| `anthropic_request_duration_seconds` | histogram | tenant | AgentLoop |

`route_template` 使用 FastAPI 的 `{tid}`/`{pid}`/`{sid}` 形式(非解析后
的 URL),保证标签基数有限。`tenant` 在未鉴权路由上默认 `unknown`。

**Trace 模型**:

- 默认是「日志 span」:`mini_cc.server.tracing` 里的
  `log_span(name, **fields)` 在退出时发一条结构化 `span.end` INFO 日志,
  含 `span` / `dur_ms` / `trace_id` + 调用方字段。零额外依赖。
- 设 `MINI_CC_OTEL_EXPORTER=otlp`(或 `jaeger` / `console`)会按需
  import OpenTelemetry SDK,额外发真正的 OTel span。缺少
  `opentelemetry-*` 包时打一条 warning,退化为仅日志模式。

**Token 归属**:

每次 Anthropic stream 完成后,`AgentLoop.run()` 读
`response.usage`,按 tenant 推四个 counter
(`input`/`output`/`cache_read`/`cache_create`)。被取消时改推
`anthropic_request_total{status="cancelled"}`。

**Prometheus 抓取示例**:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: mini_cc
    static_configs:
      - targets: ["localhost:8000"]
```

```bash
# 制造点流量,看 counter 涨
python -m mini_cc.server &
for i in $(seq 1 5); do curl -s localhost:8000/health >/dev/null; done
curl -s localhost:8000/metrics | grep http_requests_total
```

---

## 子系统地图

下面每个子系统都是 per-project(没有模块全局变量)。ProjectManager
在 `Project._assemble()` 里把所有子系统组装起来。

| 子系统                  | 用途                                                  | s20 参考     |
| ---------------------- | ----------------------------------------------------- | ------------ |
| `AgentLoop`            | 处理一轮用户输入,流式产出事件                        | s01, s20     |
| `SubprocessSandbox`    | 路径白名单 + 命令策略 + 环境变量过滤                  | s03          |
| `Hooks`                | UserPromptSubmit / PreToolUse / PostToolUse / Stop    | s04          |
| `make_permission_hook` | deny-list + 破坏性命令拦截                            | s03          |
| `make_log_hook` 等     | 审计 / 大输出 / 全事件 sink                            | s04          |
| TodoWrite              | plan-first 执行                                       | s05          |
| `spawn_subagent`       | 聚焦型单用途 loop,工具集受限                         | s06          |
| `SkillLoader`          | 按需 skill 展开                                       | s07          |
| Compaction             | tool_result_budget / snip / micro / compact_history   | s08          |
| Memory                 | per-project 持久化记忆文件                            | s09          |
| `assemble_system_prompt` | 分段式 runtime 组装                                  | s10          |
| `RecoveryState`        | 重试 / token 升级 / 模型降级                          | s11          |
| Task 系统              | subject + deps + status + owner + worktree 绑定       | s12          |
| `BackgroundScheduler`  | 慢操作异步化;通知下一轮落到对话                      | s13          |
| `CronScheduler`        | per-project 定时任务                                  | s14          |
| `MessageBus`           | per-project JSONL 邮箱                                | s15          |
| `ProtocolTracker`      | plan-approval + shutdown 握手                         | s16          |
| `TeammateSpawner`      | 线程;idle-poll 自动认领;plan-approval 闸门          | s17          |
| Worktree + `wt_ctx`    | 每任务独立 worktree;teammate 认领时 sandbox 自动切换 | s18          |
| `MCPPool`              | per-project MCP clients;工具合并进 pool              | s19          |
| Transcript-on-compact  | 丢弃前 `write_transcript` 快照                        | s20          |

**Plan-approval 闸门** 在 turn 边界生效(而不是 turn 中途)——
`submit_plan` 让模型结束当前 turn,spawner 会阻塞下一轮直到
`review_plan` 到来。相比 s20 这是文档化的简化。

**`wt_ctx` 自动切换 cwd:** teammate 自动认领带 worktree 的 task 时,
`AgentLoop.set_worktree(path)` 会把 teammate 的 sandbox 换成该 worktree。
每个 teammate 在 spawn 时会拿到自己的 `SubprocessSandbox`,所以这个
切换不会影响其他 session。

---

## 并发模型

- `SessionManager.send()` 是**同步**的,获取 per-project `RLock`。
  不同项目并行;对**同一**项目的 send 会串行化。
- HTTP 层通过 `SessionManager.try_lock()` 暴露这一点 —— 第二个并发
  send 到同一项目会返回 **409 `project_busy`**,而不是阻塞 HTTP worker。
- SSE 流通过专属 worker 线程 + `asyncio.Queue` 桥接同步 iterator。客户端
  断开会调用 `session.stop()`;loop 在下一次迭代边界退出。

---

## Session 生命周期(跨重启续接)

一个 session **冷(cold)** 是指它只在磁盘上、当前进程内没有对应的
`AgentLoop`;**热(warm)** 是指 `AgentLoop` 已经把它的对话历史载入内存。
Messages、todos 和 session index 都落在 `<state_root>/<project_id>/`
下;`AgentLoop` 构造时会从盘上重新加载。

三种续接路径:

1. **send 时自动续接。** `POST /sessions/{sid}/send` 透明地 warm 一个
   冷会话 —— 无需额外往返。只有当内存和盘上都没有时才 404。
2. **幂等 start。** `POST /sessions` 传入已存在的 `session_id` 时返回
   **200**(re-warm)而不是 409。适用于"存在就打开,不存在就创建"。
3. **显式 resume。** `POST /sessions/{sid}/resume` warm 一个冷会话并
   返回它的 `SessionMeta`。适合在第一次 send 之前预热(比如刚重启
   server 之后)。

`GET /sessions` 返回每个盘上 session 的 `SessionMeta`:

```json
[{
  "session_id": "s1",
  "created_at": "2026-06-20T22:11:08.110Z",
  "last_active_at": "2026-06-20T22:14:42.009Z",
  "message_count": 14,
  "in_memory": true
}]
```

索引文件位于 `<state_root>/<project_id>/sessions/index.json`。对于
该特性之前创建的项目,首次读取时会从 `messages/*.json` 文件懒重建
索引。

**中途崩溃恢复:** 如果 server 在 turn 中途死掉,对话尾部可能是一个
带 `tool_use` 块、但没有对应 `tool_result` 的 assistant 消息。warm
时 loop 会追加一条合成的 user turn,为每个悬挂的 tool_use 补一条
`tool_result`,内容为 `[interrupted by server restart]`,
`is_error: true`。这样既不丢上文,也能让模型继续推进。

---

## 交互式权限

按项目可选开启。在工作区放一个
`<workspace>/.mini_cc/permissions.toml`:

```toml
prompt_tools = ["bash", "fs_write", "fs_edit"]
timeout_seconds = 300   # 可选;默认 300
```

当 loop 即将调用 `prompt_tools` 中列出的工具时,它会:

1. 向 SSE 流发送一条 `permission_request` 事件:
   ```json
   {"type": "permission_request", "request_id": "<hex>",
    "tool_name": "bash", "tool_input": {"command": "..."},
    "id": "<tool_use_id>"}
   ```
2. 阻塞,直到客户端 POST 决定到
   `/permissions/{req_id}/decide`,或超时,或会话停止。
3. `allow` → 执行工具;`deny` → `message`(或 `[permission timed out]`)
   作为 `tool_result` 内容返回给模型。

客户端重连后可以用 `GET /permissions` 拉取当前待处理请求。

没有配置文件时:无 interceptor、无提示,就是今天的行为。静态的
`make_permission_hook`(deny-list + destructive 拦截)依然独立生效。

---

## 测试

本框架带 289 个通过的测试 + 24 个子测试(pytest)。s20 的 mock 模式
镜像在 `tests/test_p0_*.py`。

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

每个 P3 server 测试都用 FastAPI 的 `TestClient` 配合 stub Anthropic
client,不打网络、不发真实 API。覆盖范围:

- 鉴权:缺 key / 未知 key(401)、租户不匹配(403)
- 项目 CRUD:create / list / get / delete / 重复(409)
- ID 校验:路径穿越尝试被拒(400)
- 跨租户隔离:其他租户拿到 404(不泄漏存在性)
- 会话:start / list / remove(未知 404)
- SSE:事件顺序、error 事件转发、generator 异常后的清理、哨兵
- 并发:项目锁被持有时返回 409 `project_busy`

---

## Web UI(P4)

`mini_cc/web/` 下是一套 Devin 风格的深色 React 前端。它直接走上面
列出的 HTTP/SSE 接口,没有额外的 API。

**技术栈**:Vite + React 19 + TypeScript + Tailwind + Zustand +
React Router。e2e 用 Playwright。

### 本地运行

开发后端监听 `127.0.0.1:8002`(8001 被开发机器上别的服务占用,
前端默认 API base 也跟着用 8002)。LiteLLM 作为 Anthropic 兼容代理
监听 `:8000`,转发到 `glm-4.7` 之类模型。

```bash
# 一次性
pip install -r requirements.txt              # 含上传用的 python-multipart
cd mini_cc/web && npm install

# 终端 1 —— 后端(端口 8002)
python -m mini_cc.server keygen my_tenant    # 输出 mck_<hex>
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
MINI_CC_ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
MINI_CC_ANTHROPIC_API_KEY=any-fake-key \
python -m mini_cc.server

# 终端 2 —— 前端(端口 5173)
cd mini_cc/web && npm run dev
```

打开 http://localhost:5173,用 `keygen` 输出的 `mck_<hex>` 登录。
登录表单会从 key 自动解析 tenant。

### 功能

- 顶栏的**租户切换**;profile 存在 `localStorage`。Tenants 页可
  管理 / 显示 / 删除 key。
- **项目列表**,支持新建 / 删除 / 打开。
- **工作区**有三个 tab:
  - **Chat** —— SSE 流式助手回复;可折叠的活动卡片(tool_use /
    tool_result)默认收起,点击展开。
  - **Files** —— 递归文件树,右键菜单:上传文件、上传文件夹
    (通过 `webkitdirectory` 保留结构)、新建文件夹、预览、删除。
    支持下载项目 ZIP。
  - **Sessions** —— 列表、打开、删除。

### Playwright e2e

```bash
cd mini_cc/web
MINI_CC_DATA_DIR=$PWD/../mini_cc_data_e2e npm run e2e
```

`e2e/globalSetup.ts` 会针对运行中的后端预配置一个全新的 `e2e`
租户 + key + 项目(`e2e_proj`)。四条用例覆盖鉴权、项目创建、
文件上传 + 预览 + zip 下载、以及通过 LiteLLM 的流式对话。

---

## 范围外(有意延后)

下面这些**不是 bug** —— 是有意切掉的。捡起来之前先开个 issue,对齐一下
范围。

- **WebSocket 传输。** 暂时只有 SSE。
- **容器级沙箱隔离(P5)。** 目前的 sandbox 只是 defense-in-depth 软层;
  对不受信代码,请把 mini_cc 跑在容器里。
  `python -m mini_cc.server`。

---

## License

MIT.
