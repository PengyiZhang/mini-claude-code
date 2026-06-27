# mini_cc 生产化加固路线图

> **For Claude:** 本文档是审阅 + 路线图，不是按 task 逐条 TDD 的实施计划。
> 实际实施时，每个 P0/P1 项应再拆为独立 plan（带 failing test → minimal impl → commit）。

**Goal:** 把 mini_cc 从「功能完备的原型」推进到「可在生产承载多租户负载的稳定服务」。

**生成时间:** 2026-06-28
**当前状态:** dev-opensandbox 分支，783 测试通过，Phase A–J + P5 + P6 全部落地。
**审计来源:** 安全/可靠性/性能 三路并行 audit（subagent 读源码得出，关键论断人工抽样验证）。

---

## 0. 当前态势评估

### 已具备的生产基因

- **多租户路由层鉴权闭环**——`server/deps.py:require_scope` 把 401/403 + expiry + tenant + scope 四项检查前置到所有业务路由之前。
- **双层可替换接口**——`Sandbox` / `ContainerRuntime` / `Tool` 三条接口轴让换实现不动调用方。
- **项目装配缓存**——`projects/manager.py:get` 暖路径 ~1μs；按 `.mcp.json`/`mcp.toml`/`permissions.toml` 的 mtime 签名自动失效。
- **三级沙箱后端 + 自动降级**——opensandbox → docker → subprocess；不可用时静默回退并记 `DegradeEvent`。
- **结构化 tracing + 基础 metrics**——HTTP 请求计数/延迟、Anthropic token 归因、per-tenant 聚合。
- **783 测试覆盖 + TDD 文化**。

### 距离生产级的六大核心差距

1. **多租户隔离纵深不足**——路由层把守好，但 fs / sandbox / MCP 层有可绕过的逃逸面。
2. **资源生命周期断裂**——MCP / LSP / 容器子进程在多种边界路径下不回收。
3. **持久化原子性**——多数 `save_*` 不是 temp+rename，崩溃即丢。
4. **单进程状态绑定**——warm AgentLoop / Project cache / locks / containers 都在内存，无法横向扩展。
5. **同步 IO 阻塞 event loop**——SSE 流式 + docker shell-out + urllib 都在阻塞调用里。
6. **可观测性盲区**——工具失败率、MCP 重连、缓存命中、锁等待都没指标。

---

## 1. P0：上线前必须修复（安全 + 数据完整性）

### S1. 路径越界的纵深防御

- **现状**：`sandbox/subprocess_sandbox.py:56-68` 的 `resolve_path` 与 `validate_path` 分两步走，`read/write/edit` 各自调一次再 `open`，攻击者可在两条语句之间把目标替换为 symlink（TOCTOU）。
- **攻击场景**：租户在自己的 workspace 里放一个名为 `foo.txt` 的普通文件通过 `validate_path`，紧接着替换为指向 `/etc/passwd` 或另一租户 workspace 的 symlink，被后续 `read_text()` 跟随。
- **修复方向**：所有 fs 操作改为「`os.open(path, O_RDONLY|O_NOFOLLOW)` 拿 fd → `os.fstat` → 在 fd 上 read」的原子模式；Windows 用 `CreateFile` + `FILE_FLAG_OPEN_REPARSE_POINT`。`resolve_path` 不再独立暴露，并入 `validate_path`。
- **测试**：写一个用 `os.symlink` 在 validate 与 open 之间替换的并发用例（threading + event 同步）必须抛 `PathEscapeError`。

### S2. `shell=True` 命令注入面

- **位置**：`sandbox/subprocess_sandbox.py:194`——Windows 上找不到 bash 时回退到 `subprocess.run(command, shell=True)` 走 `cmd.exe`。
- **风险**：用户提供的 command 字符串未做 shell 转义；`Policy.scan_command` 的黑名单可被 `\n`、`$()`、`` ` `` 等元字符绕过。
- **修复方向**：
  - Windows 强制要求 WSL/bash；找不到则**拒绝执行**而不是退到 `cmd.exe`。
  - `Policy.scan_command` 显式拒绝 `\n` / `\r` / `` ` `` / `$(`；env 变量在 scan 之前展开。
- **测试**：包含换行、命令替换、env 注入的命令字符串必须被拒。

### S3. `ProjectManager.get(tenant_id=None)` 全局扫描

- **位置**：`projects/manager.py:207-212`——`tenant_id is None` 时走 `find_meta()` 全局扫所有租户目录。
- **风险**：任何路由忘传 tid，攻击者知道目标的 `project_id` 即可跨租户读元数据；删除接口更危险。
- **修复方向**：`get`/`delete`/`list` 在 `tenant_id is None` 时一律 raise（破坏性调用必须显式传 tid）；仅保留 migrate 等内部工具用 `find_meta`。
- **测试**：`pm.get(pid)` 不带 tid 必须 raise；`pm.get(pid, tenant_id=tid)` 跨租户访问另一租户 pid 必须 raise `KeyError`。

### S4. 持久化原子性

- **现状**：`storage/fs.py` 只有 `sessions/index.json` 走 `_atomic_write_json`（temp+rename）；`save_messages`/`save_todos`/`save_workflow` 等直接覆盖写。
- **风险**：进程崩溃在写一半 → 磁盘上留下不完整 JSON，下次加载抛异常 → **整段会话/工作流丢失**。
- **修复方向**：所有 `FSStorage.save_*` 统一 `temp file + os.replace` 原子写；`load_*` 在 JSON 解析失败时尝试加载 `.bak`。
- **测试**：模拟写中途进程死亡（mock `os.replace` 抛 `InterruptedException`），下次 load 不丢数据。

### S5. API key 存储 + 比较

- **位置**：`auth/keys.py`——明文存盘 + 字符串比较。
- **风险**：磁盘泄露 = 所有租户 key 泄露；时序攻击可推断有效 key 前缀。
- **修复方向**：
  - 存储 `sha256(key + per-tenant salt)` 或 argon2id；原始 key 仅创建时返回一次。
  - `lookup` 用 `hmac.compare_digest`。
  - `keys.json` 文件权限 0600；启动时检查并警告。
- **测试**：hash 一致性；`compare_digest` 路径；旧明文格式自动迁移。

### S6. 容器 `extra_mounts` 白名单

- **位置**：`sandbox/config.py:156-165`——当前允许挂任意宿主路径。
- **风险**：租户 `sandbox.toml` 配 `extra_mounts = ["/etc:/etc:ro"]` 即可在容器里读到宿主 shadow 文件。
- **修复方向**：默认拒绝 `/etc`、`/root`、`/home`、`/var/lib/docker`、`<data_dir>`、`/proc`、`/sys`；只放行运维显式配置的 allow-list（env `MINI_CC_EXTRA_MOUNTS_ALLOW`）。
- **测试**：黑名单路径配置加载时 raise；allow-list 之外路径 raise；allow-list 内 OK。

---

## 2. P1：上线后第一波加固（可靠性 + 可观测性）

### R1. MCP / LSP / 容器 子进程生命周期闭环

- **MCP**：项目缓存 mtime 失效时旧 `MCPPool` 必须 `close()`——当前实现只换新不回收旧。`Project.delete` 必须 close MCP / LSP / BackgroundScheduler；加 `atexit` 兜底。
- **容器**：`ServerRuntimeContext.shutdown` 改 graceful（30s wait → SIGKILL），不要硬杀。当前 `mgr.stop()` 直接 kill 容器内所有进程。
- **LSP**：`LSPManager` 子进程目前只在 server shutdown 时回收，project 删除时不触发——长跑后堆积大量 idle 语言服务器。
- **测试**：装配 → 失效 → 重新装配后，旧 MCPPool 的 transport `is_closed() == True`；`pm.delete(pid)` 后无残留子进程（用 psutil 验证）。

### R2. AgentLoop 错误状态机

- **现状**：`core/loop.py:466-478` 把 LLM/工具错误直接作为消息追加进 `self.messages`，污染对话历史。
- **风险**：LLM 限流错误重试成功后，错误信息仍留在历史里，模型可能基于错误信息做错误决策；形成「错误循环」。
- **修复方向**：引入 `ErrorBlock` 元数据块（不计入 LLM 上下文，或显式 `transient=true`）；prepare_context 时剔除或单独标。重试成功后必须把上一轮的 ErrorBlock 撤回。
- **测试**：制造 429 重试成功场景，验证最终 messages 中无 ErrorBlock。

### R3. SSE 心跳 + 客户端断开检测

- **现状**：`server/sse.py` + `server/routes/sessions.py:180-201` 依赖 `on_cancel`；网络分区下服务端空转 LLM turn。
- **修复方向**：
  - 每 30s 发一条 `:keep-alive` SSE 注释行；客户端可选回 PONG。
  - 超时（如 90s 无活动）即 cancel AgentLoop 并 close 连接。
  - SSE 长连接纳入限流——按时间窗累计计费，而非一次 token。
- **测试**：模拟客户端断开（取消 generator），验证 AgentLoop 在 N 秒内被 cancel、容器 exec 被 kill。

### R4. 重试策略区分错误类型

- **位置**：`core/recovery.py:35-69`——当前对 429 一律退避重试。
- **风险**：quota 耗尽 / billing 失效时框架会一直重试到天荒地老，还可能被 provider 封禁。
- **修复方向**：识别 provider 特定错误码（Anthropic `credit_overage` / `authentication_invalid`、OpenAI `insufficient_quota` / `invalid_api_key`）直接失败 + 上报告警；只对临时限流（429 + `retry-after`）和网络错误重试。
- **测试**：注入 quota 错误，验证不重试 + 抛特定异常类型。

### R5. 可观测性指标补全

- **现状**（已覆盖）：HTTP 请求计数/延迟、Anthropic token、per-tenant 聚合。
- **缺**：
  - 工具失败率（按 `tool.name` 分组）
  - MCP 连接失败 / 重连 / 工具调用失败计数
  - 容器沙箱降级事件计数（`DegradeEvent` 已有，但未导出 metrics）
  - 后台任务队列深度 + 失败计数
  - Project / MCP / LSP 缓存命中率
  - per-project lock 等待时长分布（histogram）
  - 会话活跃时长 + cold/warm 装配耗时分布
  - LLM provider 维度区分（litellm 多 provider 下需要按 provider 分组 token）
- **修复方向**：在现有 `server/metrics.py` 基础上补 `Counter` / `Histogram`；工具调用包装一层 `with metrics_tool_latency(...)`；缓存层加 hit/miss counter。
- **测试**：每个新指标的 smoke test，验证 label 正确。

### R6. `/readyz` 探针

- **现状**：`/health` 仅返回固定 200，不检查任何下游。
- **修复方向**：
  - `/health`：进程存活（保持简单）。
  - `/readyz`：LLM provider ping（轻量 models/list）、storage 可写探针、docker/OS runtime 可达探针；任一失败返回 503 + 错误详情。
- **测试**：mock 各下游不可用，验证 readiness 切换。

### R7. 配置敏感面

- **tracing**：默认不记 prompt / tool args 内容；如必须记，做 secret scrub——regex 替换 `sk-...`、`Bearer ...`、`password=...`、`Authorization: ...`。
- **OpenSandbox TLS**：默认强制 HTTPS；`OPEN_SANDBOX_PROTOCOL=http` 时除非显式设 `OPEN_SANDBOX_ALLOW_INSECURE=1`，否则警告并要求确认。
- **日志**：所有 log line 加 `tenant_id`/`project_id`/`session_id`/`trace_id` 字段，便于多租户排查；绝不打印 prompt 全文。
- **测试**：scrub regex 单测覆盖常见 secret 格式。

### R8. Key rotation grace 期降级

- **位置**：`auth/keys.py:157-203`——grace 期内旧 key 保留原 scope。
- **风险**：泄露的旧 key 在 grace 期内仍可做 `admin:write`。
- **修复方向**：grace 期开始即把旧 key scope 降级到 `*:read`（或自定义 `legacy_grace_scope`）；记录旧 key 使用次数，异常时提前撤销。
- **测试**：rotate 后旧 key 写操作必须 403。

---

## 3. P2：横向扩展准备（架构演进）

### X1. 无状态化 API 节点

单进程下，warm AgentLoop / Project cache / `_lock_for` / `_container_mgrs` / BackgroundScheduler / CronScheduler / MCPPool / SSE 推送 **全部在内存**。多实例部署前必须迁移：

| 状态 | 当前 | 目标 |
|---|---|---|
| warm AgentLoop | `SessionManager._sessions` 内存 | turn 一结束即卸载，状态全外置到 storage；冷启动装配优化到 <100ms |
| `_lock_for(project_id)` | 进程内 threading.Lock | Redis 分布式锁 `SET key value NX PX ttl` |
| Project 缓存 | 进程内 dict + mtime 失效 | 进程内仍缓存；失效信号走 Redis pub/sub，所有实例同步 invalidate |
| BackgroundScheduler | 进程内线程池 | 独立 worker 进程，从 Redis 队列消费 |
| CronScheduler | 进程内 tick loop | 独立 scheduler 进程；多实例用 leader election（Redis SETNX） |
| 容器 ↔ 实例亲和 | docker 模式天然绑定 | docker 模式用 sticky session 或迁移到 OpenSandbox（远端管理） |
| SSE 推送 | 进程内 yield | Redis pub/sub；任何实例发事件，任何实例的连接都能收 |

### X2. 沙箱 exec 性能

- 每次 `docker exec` 冷启 50–200ms，单 turn 多工具时累积成 5–20s。
- **短期**：`docker exec` 复用——保持长 session、批量执行；OpenSandbox 用长连接 HTTP（urllib → httpx with pool）。
- **中期**：容器内常驻 exec agent（Unix socket 或 gRPC 流），把多次 exec 复用到一条连接。

### X3. 长会话存储

- `web/src/lib/store.ts:rawToChatMessages` + `storage/fs.py:load_messages` 都是 O(n) 全量加载；5000 条会话每次 50MB+ JSON、500ms+ 反序列化。
- **方案**：分段 messages（每 100 条切块独立文件）+ 滑动窗口加载（最近 N 条进内存）+ 历史摘要压缩（超 N 条的旧消息用 LLM 摘要替代）。

### X4. 同步 IO 移出 event loop

- `sandbox/runtime.py:92` 的 `subprocess.run`、`mcp/http.py:68` 的 `urllib.request.urlopen`、所有 `FSStorage` 文件 IO 都阻塞 event loop。
- **方案**：用 `run_in_executor`（共享池 + 上限）或改原生异步（`asyncio.subprocess`、`httpx.AsyncClient`、`aiofiles`）。
- SSE 当前每连接一个 `ThreadPoolExecutor(max_workers=1)`——改共享池（如 32 workers）+ 任务队列。

### X5. 前端长列表

- Zustand store 每次 update 创建新数组，React 重渲染整个消息列表。
- **方案**：
  - 虚拟滚动（如 `@tanstack/react-virtual`），仅渲染可见区域。
  - 单条 `MessageBubble` 用 `React.memo` + stable key。
  - text delta buffer 节流（50ms flush 一次），避免每 token 重渲染。

### X6. 容器密度与 idle 回收

- 一租户一容器 → 1000 租户 = 1000 容器，内存压力巨大。
- **方案**：idle 检测（N 分钟无 exec 调用即 stop，仅保留 image）；下次请求按需拉起。配合 P5 的 `DegradeEvent` 机制做冷启延迟监控。

---

## 4. 最先撞到的天花板（TOP 3）

1. **多租户隔离纵深不足（S1–S6）** —— 一次安全事件即可全盘失守。**这是上线「不能等」的项**。
2. **单进程状态内存绑定（X1）** —— ~50 QPS 即触顶，无法靠加机器解决。
3. **MCP/LSP 子进程泄漏（R1）** —— 长跑生产环境下缓慢泄漏文件描述符，几周后开始随机失败。

---

## 5. 建议推进顺序

| 阶段 | 工期 | 内容 | 出口标准 |
|---|---|---|---|
| **阶段 0：上线前安全** | 1 周 | S1–S6 全部 P0 + 单元/集成测试 | 安全测试集全绿；外部安全 review 通过 |
| **阶段 1：可靠性 + 可观测** | 1 周 | R1 资源生命周期 + R3 SSE 心跳 + R4 重试分级 + R5 metrics 补全 + R6 readyz + R7 配置敏感面 | 24h soak test 无资源泄漏；指标 dashboard 覆盖 |
| **阶段 2：横向扩展地基** | 2–3 周 | X1 抽离锁/调度/事件到 Redis；warm session 重构为冷启动 | 双实例部署 + 负载均衡 soak test 通过 |
| **阶段 3：性能/容量** | 按需 | X2 沙箱连接复用 / X3 长会话 / X4 异步化 / X5 前端 / X6 idle 回收 | 单 turn 50 工具调用 < 3s；5000 条会话加载 < 200ms |

每阶段完成后：
- 跑 `pytest -k "p0|p1|p2|p3|p4|p5|p6"` + 阶段新加的测试集，保持 783+ 测试绿。
- 更新 `docs/container-sandbox.md` 与 `mini_cc/ARCH.zh.md` 中相关章节。
- 在 `MEMORY.md` 的 `project_mini_cc.md` 追加「Hardening 阶段 N 已发版」快照。

---

## 6. 验证手段补充

### 安全测试集（阶段 0 新建）

`tests/security/`：
- `test_path_traversal.py` —— symlink、`..`、绝对路径、Windows 短名、TOCTOU
- `test_command_injection.py` —— 换行、`$()`、`` ` ``、env 展开
- `test_tenant_isolation.py` —— 跨租户 fs / session / container / MCP 访问
- `test_key_storage.py` —— hash 一致、compare_digest、迁移、文件权限
- `test_mount_whitelist.py` —— 黑/白名单路径

### 可靠性测试集（阶段 1 扩充）

`tests/reliability/`：
- `test_soak.py` —— 模拟 1h 多租户混合负载，验证 FD / 内存 / 子进程数稳定
- `test_graceful_shutdown.py` —— SIGTERM 后 30s 内退出且无残留
- `test_sse_disconnect.py` —— 客户端断开后服务端资源回收
- `test_atomic_persistence.py` —— 模拟中途崩溃，数据完整

### 性能基准（阶段 3 新建）

`benchmarks/`：
- 单 turn N 工具调用延迟曲线
- 消息历史加载耗时 × 长度
- 并发 SSE 连接数 × 内存占用
- LLM token 吞吐量

---

## 7. 不在本路线图内（明确不做）

- **gVisor / Kata / Firecracker 强隔离** —— 沙箱层接口已支持，作为运维侧选择；mini_cc 不内置。
- **Web UI 重写** —— 当前 React 够用，仅做性能优化（X5）。
- **新业务功能** —— 路线图只关注「让现有功能稳得住」；新特性（如 video generation、agent marketplace）暂停。
- **多语言 SDK（Python 之外）** —— 维持单 host 语言。

---

## 8. 关联文件速查

| 关注点 | 文件 |
|---|---|
| 路径校验 | `sandbox/subprocess_sandbox.py:55-68` |
| 命令执行 | `sandbox/subprocess_sandbox.py:185-200`、`sandbox/policy.py` |
| 跨租户隔离 | `projects/manager.py:207-226`、`server/deps.py:require_scope` |
| 持久化 | `storage/fs.py:74-78, 120-134, 211-213` |
| 鉴权 | `auth/keys.py:86-134, 149-211`、`auth/scope.py` |
| 容器挂载 | `sandbox/config.py:156-165`、`sandbox/manager.py:40-46` |
| 资源生命周期 | `mcp/stdio.py:104-155`、`mcp/client.py:248-249`、`server/runtime_context.py:132-138`、`lsp/__init__.py:84-136` |
| 错误处理 | `core/loop.py:466-478`、`core/recovery.py:35-69` |
| SSE | `server/sse.py`、`server/routes/sessions.py:180-201` |
| Metrics | `server/metrics.py:282-323`、`server/tracing.py:86-132` |
| 单进程状态 | `session/manager.py:38`、`projects/manager.py:131`、`tools/background.py`、`scheduler/cron.py` |
