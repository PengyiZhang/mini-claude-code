[ < [06](../en/06-placeholder.md) ] [ [08](08-sse-streaming.md) > ] · [English version](../en/07-http-server.md)

# 07 — HTTP 服务与路由

> `build_app` 是一个纯工厂函数:它接收已经装配好的 SDK
> (`ProjectManager`、`SessionManager`、`TenantKeyRegistry`),
> 返回一个 FastAPI 应用。服务端本身不持有任何业务状态——它只是一层薄薄的传输。
> 本章覆盖 lifespan 契约、中间件栈顺序、所有路由共享的租户边界校验模式、
> `MiniCCError` 错误信封,以及按租户计量的令牌桶限流器。

---

## 问题与动机

s20 提供了一个 Python SDK(`SessionManager.send()` 返回事件的同步迭代器)。
这种形状在 Python 进程内很好用,对浏览器、curl 用户或异语言后端集成毫无用处。
HTTP/SSE 传输层(服务端 Phase A → Phase E)存在的目的,
就是把这套 SDK 安全地暴露给多租户外部调用方。

"安全"在这里拆成四个硬需求,驱动了 `mini_cc/server/` 里每一个设计决策:

1. **租户隔离**。两个租户共享一个进程时,绝不能读到对方的项目、会话、对话记录——
   哪怕某个开发者手抖漏了一个校验。校验必须是*机械的、统一的*,而不是"每个路由各自记得"。
2. **统一的错误形态**。所有失败路径——404、409、429、SDK 抛出的 `KeyError`——
   都必须渲染成同一份 JSON 信封,这样客户端只需写一个错误处理器。
3. **可观测**。每个请求都要有 trace id(让日志能拼接起来)以及 RED 指标
   (Rate / Errors / Duration),按租户和路由模板打标。
4. **优雅退出**。直接杀进程会让 Anthropic 客户端线程卡住、MCP stdio 子进程泄漏。
   lifespan 关闭阶段必须按顺序:停会话、回收 teammate、断开 MCP 池、销毁容器。

`mini_cc/server/app.py` 中的"工厂 + lifespan + 中间件 + 依赖"模式就是这四点的回答。

---

## 设计与原理

### 是工厂,不是模块级全局

`build_app`(`mini_cc/server/app.py:98`)从显式传入的依赖构造 FastAPI 应用。
模块导入阶段既不碰文件系统也不绑端口。测试里每个场景用桩 manager 构造一个 app;
生产里 `cmd_serve`(`mini_cc/server/cli.py:114`)只构造一个。

```
        cmd_serve (cli.py)
              │
              ▼
   ServerRuntimeContext ──► build_project_manager() ──► ProjectManager
        │                                                │
        │                                                ▼
        │                                          SessionManager
        ▼
   TenantKeyRegistry ──┐
   TenantRateLimiter ──┼──► build_app(data_dir, key_registry, pm, sm,
   MetricsRegistry   ──┘         cors_origins, rate_limiter,
                                 metrics_registry, server_runtime)
                                         │
                                         ▼
                                    FastAPI
```

所有 manager 最终落到 `app.state`,依赖注入辅助函数
(`get_pm`、`get_sm`、`get_registry`,见 `deps.py`)再把它们取出来。

### Lifespan:有序退出

`lifespan`(`app.py:57`)在启动时 yield 一次(顺带警告不安全的默认密钥),
然后在关闭阶段按精心设计的顺序执行四轮清理:

1. **停掉每个活跃会话**(`sess.stop()`)。设置 loop 的取消标志,
   让正在跑的 Anthropic 调用能返回而不是卡死。
2. **关闭 teammate spawner**(`spawner.shutdown(timeout=5.0)`)。
   每个 TeammateSpawner 通过 bus 发送 shutdown_request 并 join 工作线程,
   避免邮箱写入半途丢失。
3. **断开 MCP 池**(`pool.disconnect_all()`)。否则 stdio MCP 子进程和
   HTTP 连接池在重启之间泄漏。
4. **停掉按租户缓存容器**(`ctx.shutdown()`)。对每个缓存租户调用
   `TenantContainerManager.stop()`。

每一轮都包在 `try/except` 里——一个脏会话不能让其余退出中止。

### 中间件栈:顺序很重要

```
   request  ──►  TraceIdMiddleware      (最外层:分配 X-Trace-Id)
                ─► MetricsMiddleware    (RED 指标,原生 ASGI)
                   ─► CORSMiddleware    (放行 Last-Event-Id 头)
                      ─► router
```

`TraceIdMiddleware`(`middleware.py:25`)第一个加入,因此包裹了下方一切——
连 CORS 拒绝的日志行都能带上 trace id。`MetricsMiddleware`(`middleware.py:46`)
特意用原生 ASGI(而非 `BaseHTTPMiddleware`),就是为了能读 `scope["route"]`,
把指标按路由模板(如 `/tenants/{tid}/projects/{pid}/...`)而非原始路径打标。
CORS 放行 `Last-Event-Id` 和 `X-Trace-Id`(`app.py:136`)——前者是 SSE 断线恢复必需(见第 08 章)。

### 租户边界校验模式

每个受保护的路由形态一致,且校验是机械的——漏掉的路由根本过不了 code review:

```python
# mini_cc/server/routes/sessions.py:31 (简化版)
@router.post("", response_model=SessionOut)
def start_session(body: CreateSessionRequest,
                  tid: str = Depends(require_scope("sessions:write")),  # 1
                  pm=Depends(get_pm), sm=Depends(get_sm)) -> SessionOut:
    validate_id(pid)                          # 2 — 纵深防御的 ID 校验
    _check_project_tenant(pid, tid, pm)       # 3 — 项目存在 + 属于 tid
    ...
```

三层校验,各捕获一类 bug:

1. **`require_scope(required)`**(`deps.py:56`)解析 bearer token、查注册表、
   校验 key 的 `tenant_id == tid`、检查 key 的 scope 覆盖所需 scope。
   不匹配 → 403,缺失/过期 → 401。
2. **`validate_id`**(`deps.py:98`)拒绝任何非 `[A-Za-z0-9_-]+` 的路径 ID——
   在 SDK 看到值之前就堵死用 `project_id` 做路径穿越的攻击。
3. **`_check_project_tenant`**(`sessions.py:20`)重新拉项目并断言
   `p.meta.tenant_id == tid`。即使调用方猜到了别的租户的 `pid`,
   也只会得到 404——绝不泄漏。`workflow_v2.py:_service_for` 和
   `webhooks` 路由使用相同模式。

### `MiniCCError` 错误信封

路由内抛出的所有异常都继承自 `MiniCCError`(`errors.py:13`)。
`build_app` 里的两个异常处理器(`app.py:142`、`app.py:155`)
把它们全部渲染成同一份 JSON:

```json
{"error": {"code": "rate_limited", "message": "...", "details": {...}}}
```

`map_sdk_exception`(`errors.py:63`)把 SDK 原始异常翻译过来——
`KeyError` → 404,`ValueError("already exists")` → 409,其他 `ValueError` → 400——
路由处理器只需 `raise map_sdk_exception(e)`,剩下交给信封机制。429 处理器
额外从 `request.state.rate_limit_retry_after` 取出 `Retry-After` 回写响应头。

### 按租户令牌桶限流器

`TenantRateLimiter`(`ratelimit.py:45`)是一个线程安全的内存令牌桶,按租户为键。
默认 60 RPM,容量 = RPM(允许租户瞬间用掉整分钟的配额),补充速率 = RPM/60 每秒。
桶在首次请求时惰性创建;按租户覆盖来自 `MINI_CC_RATE_LIMIT_RPM=tenant=rpm,...`。

`_apply_rate_limit`(`deps.py:143`)通过 `check_rate_limit_scope(required)` 串入——
 同一个依赖既做 scope 校验又消费一个令牌。这种组合就是为什么路由只声明
 一个 `Depends(...)`,而不是两个。

多进程部署时 `make_rate_limiter`(`ratelimit.py:144`)优先使用 `RedisRateLimiter`
(按租户的 60 秒固定窗口,INCR + TTL)。Redis 在构造期不可达就回退到内存限流器并打 warning——
服务还能启动,只是多进程部署失去共享计数器。

---

## 操作与配置

### 环境变量(除特别标注外均在 `cli.py` 读取)

| 环境变量 | 默认值 | 含义 | 来源 |
|---|---|---|---|
| `MINI_CC_HOST` | `127.0.0.1` | 绑定主机 | `cli.py:118` |
| `MINI_CC_PORT` | `8000` | 绑定端口 | `cli.py:119` |
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | 项目与 keys.json 根目录 | `cli.py:23` |
| `MINI_CC_CORS_ORIGINS` | `*` | 逗号分隔的允许来源 | `cli.py:111` |
| `MINI_CC_RATE_LIMIT_RPM_DEFAULT` | `60` | 默认每租户 RPM | `cli.py:36` |
| `MINI_CC_RATE_LIMIT_RPM` | (空) | `tenant=rpm,tenant=rpm` 覆盖项 | `cli.py:38` |
| `MINI_CC_LOG_FORMAT` | `json` | `json` 或 `text` | `cli.py:69` |
| `MINI_CC_LOG_LEVEL` | `INFO` | 日志级别 | `cli.py:70` |
| `MINI_CC_WEB_DIST` | (空) | 覆盖 React UI 构建产物路径 | `app.py:210` |

### CLI 子命令(`python -m mini_cc.server ...`)

| 子命令 | 效果 |
|---|---|
| `serve`(或无参数) | 在 `HOST:PORT` 上运行 uvicorn |
| `keygen <tid> [--scopes S] [--expires-in 7d] [--label L]` | 生成 API key |
| `keys list <tid>` | 列出该租户所有 key(含过期) |
| `keys rotate <key> [--grace-hours N]` | 轮换 key,可选宽限 |
| `revoke <key>` | 硬撤销 key |
| `sandbox status [--tid T]` / `stop T` / `build-image` | 容器生命周期 |

### 元端点

| 端点 | 鉴权 | 用途 |
|---|---|---|
| `GET /health` | 无 | `{ok, llm_configured}` —— 就绪探针 |
| `GET /metrics` | 无(可信网络) | Prometheus 0.0.4 文本 |
| `GET /metrics.json` | 无(可信网络) | JSON 快照 |
| `GET /tenants/{tid}/admin/metrics.json` | `admin:read` | 按租户指标 |

### `build_app` 中注册的路由组

`projects`、`sessions`、`resources`(含 `download_router`)、`permissions`、
`admin`、`commands`、`run_table`、`share_router`(sessions)、`webhooks`,
以及 `workflow_v2.ALL_ROUTERS`(`definitions_router`、`runs_router`)。
SPA catch-all(`/{path:path}` GET)最后注册,确保不会遮蔽真实 API 路由。

---

## 验证步骤

针对跑在 `:8002` 的后端执行(请替换为自己的租户/key)。

```bash
# 1. 就绪检测 —— /send 之前 llm_configured 必须为 true。
curl -s http://127.0.0.1:8002/health
# {"ok":true,"llm_configured":true}

# 2. 租户边界:跨租户 pid 返回 404,而不是泄漏。
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $KEY_TENANT_A" \
  http://127.0.0.1:8002/tenants/tenantA/projects/tenantB_pid/sessions
# 404

# 3. 缺失 bearer → 401,标准信封。
curl -s http://127.0.0.1:8002/tenants/tenantA/projects | jq .
# {"error":{"code":"unauthorized","message":"missing bearer token","details":{}}}

# 4. scope 不足 → 403,带 WWW-Authenticate。
curl -s -i -H "Authorization: Bearer $READ_ONLY_KEY" \
  -X POST http://127.0.0.1:8002/tenants/tenantA/projects \
  -H 'Content-Type: application/json' -d '{"project_id":"p1"}' \
  | grep -i 'www-authenticate'
# WWW-Authenticate: Bearer scope="projects:write"

# 5. 限流:连续打满直到 429,观察 Retry-After。
for i in $(seq 1 70); do
  curl -s -o /dev/null -w "%{http_code} " \
    -H "Authorization: Bearer $KEY" \
    http://127.0.0.1:8002/tenants/tenantA/projects
done
# ... 200 200 200 429 429 ...

# 6. 指标携带路由模板 + 租户标签。
curl -s http://127.0.0.1:8002/metrics.json | jq '.counters.http_requests_total'
```

---

## 常见坑与调试

1. **"项目明明存在,我的路由却返回 404"**。先确认 bearer key 的 `tenant_id`
   与 `{tid}` 一致,*并*确认项目的 `meta.tenant_id` 也一致。
   两个校验失败时都收口到 404 以避免存在性泄漏——`Forbidden("api key does not match tenant")`
   只在路径 tid 与 key 的 tid 不一致时触发,而不是项目归属别的租户时触发。
2. **"我明明低于 60 RPM,却 429"**。桶容量等于 RPM,所以 60 个并发请求瞬间吃掉一整分钟的配额,
   第 61 个即使在第一秒内也会被拒。要么调高 `MINI_CC_RATE_LIMIT_RPM_DEFAULT`,
   要么在客户端做节流。
3. **忘了 `validate_id`**。`deps.py:24` 的正则校验是挡在
   `../../../etc/passwd` 风格的 `project_id` 与存储层之间的唯一屏障。
   每个接路径 ID 的路由都必须调用它——没有全局前置 hook。
4. **中间件顺序坑**。如果你在 `MetricsMiddleware` *之后*加新中间件,
   那一层产生的拒绝在指标里就不会带 trace id。`TraceIdMiddleware` 必须第一个加入,
   见 `app.py:124` 的注释。
5. **Redis 限流器其实是"故障抛错"……某种程度上**。`RedisRateLimiter.allow`
   在 Redis 不可用时*抛* `RuntimeError`(`ratelimit.py:136`),
   而不是静默故障开放。这是有意的——但 `make_rate_limiter` 只在构造期探活;
   进程运行中 Redis 掉线就会在请求时抛错。请监控 `ratelimit.redis_unavailable` 日志行。

---

## 延伸阅读

- 源码:`mini_cc/server/app.py`、`mini_cc/server/deps.py`、
  `mini_cc/server/errors.py`、`mini_cc/server/ratelimit.py`、
  `mini_cc/server/middleware.py`、`mini_cc/server/runtime_context.py`
- 姊妹篇:[08 — SSE 流与断线恢复](08-sse-streaming.md)、
  [09 — 认证、作用域与分享令牌](09-auth.md)
- 概念篇:`../../zh/sNN-*.md`(见章节索引)
