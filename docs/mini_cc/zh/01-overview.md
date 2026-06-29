[ < prev ] [ **02** > ] · [English version](../en/01-overview.md)

# 01 — 总览与架构

> 本系列是 mini_cc 的「高级内部实现」教程。假设你已经读过 `docs/zh/s01-s12` 的
> 概念章节,知道 AgentLoop、tool_use、context 压缩这些 Claude Code 基础。本章
> 把你从「会用」拉到「能改」:解释 mini_cc 的分层、依赖方向、以及那些「一个接口
> + 多个实现」的扩展点在哪里。

---

## 问题与动机

`s20_comprehensive/code.py` 是一份 2123 行的单文件教学参考,把 19 个子系统塞在
一个 `agent_loop()` 里。它适合学概念,但不能直接拿来当生产后端用,原因有三:

1. **没有租户概念。** 单进程跑多个用户时,他们的 workspace、messages、memory 会
   互相串。`s20` 把所有状态放在 cwd 下,谁先 `cd` 谁就改了全局。
2. **无法被后端集成。** `agent_loop()` 是一个同步阻塞函数,后端服务要用它就得自
   己包一层传输、鉴权、并发控制——而这层每个团队都在重复造。
3. **模块级全局状态。** s20 的 storage、scheduler、mcp_pool 都是模块级单例,没法
   在同一进程里跑两个独立的项目。

mini_cc 的目标是:**保留 s20 的全部能力,但重构成一个多租户、可被后端嵌入的
框架**。你既能 `import mini_cc` 当 SDK 用,也能 `python -m mini_cc.server` 拉起
一个 HTTP/SSE 服务,被任意语言驱动。关键设计决策是「**无模块级全局状态**」——
每个子系统实例都是 per-project 的,`ProjectManager._assemble()` 在装配时把一个
项目需要的全部依赖一次性 wire 起来(`mini_cc/projects/manager.py:278`)。

---

## 设计与原理

### 分层架构

mini_cc 分四层,每层只依赖下面的层,绝不反向:

```
┌─────────────────────────────────────────────────────────────┐
│ server/   FastAPI · HTTP/SSE · per-tenant API-key 鉴权      │ ← 传输层
├─────────────────────────────────────────────────────────────┤
│ session/  SessionManager(per-project RLock 串行)            │ ← 编排层
│ projects/ ProjectManager(租户元数据 + 目录布局 + 装配缓存) │
├─────────────────────────────────────────────────────────────┤
│ core/     AgentLoop · hooks · recovery · compaction         │ ← Agent 核心
│ teams/  tools/  skills/  mcp/  scheduler/                   │
├─────────────────────────────────────────────────────────────┤
│ sandbox/  Sandbox Protocol + Subprocess/Container 实现      │ ← 隔离 + 存储
│ storage/  Storage Protocol + FSStorage                      │
│ auth/     TenantKeyRegistry(与传输无关)                    │
└─────────────────────────────────────────────────────────────┘
```

注意:**SDK 核心(core/、tools/)对 HTTP 传输层零感知**。`AgentLoop` 不知道自己
是被 SDK 直接调用,还是被 FastAPI 路由通过 SessionManager 调用。这是双向自由:
你可以把 mini_cc 嵌进自己的 Python 后端做 in-process 调用,也可以独立部署。

### 一次请求的主链路

下图是「Web UI 发一句话」的完整生命周期。它穿过四层,最终回到客户端:

```
client ─POST /send──▶ routes/sessions.py
                       │
                       ▼  Depends(require_scope("sessions:write"))
                     deps.py ── 解析 Bearer key
                       │       → 校验 expiry → tenant 匹配 → scope
                       ▼
                     SessionManager.send(project_id, session_id, input)
                       │
                       ├── _ensure_warm()  冷会话?从盘上重建 AgentLoop + 历史
                       │
                       └── with _lock_for(project_id):  同项目串行
                             │
                             ▼
                           AgentLoop.run(user_input)
                             │  loop: 组装 context → stream LLM → 执行工具
                             │
                             ├──▶ tool.handle(ctx, args)
                             │      └──▶ ctx.sandbox.execute(...)  或 .read()
                             │
                             └──▶ yield event  (text / tool_use / tool_result / done)
                                  │
                                  ▼  SSE 桥接(worker 线程 + asyncio.Queue)
                                client
```

几个关键不变量:

- **同一个项目的路由和热会话共享同一个 `Project` 对象**。`ProjectManager.get()`
  带缓存(`mini_cc/projects/manager.py:225`),按 `.mcp.json`/`mcp.toml`/
  `permissions.toml` 的 mtime 签名失效。暖路径 ~1μs;冷装配才连 MCP、建调度器。
- **同项目串行,跨项目并行**。`SessionManager.send()` 获取 per-project `RLock`
  (`mini_cc/session/manager.py:183`),保证 LLM 这种有状态调用不会并发踩同
  一个 messages 文件。
- **HTTP 层不阻塞 worker**。`SessionManager.try_lock()` 是非阻塞探针,第二个并发
  send 到同一项目直接返回 **409 `project_busy`**。

### 两个「多实现」接口

mini_cc 最值得理解的扩展点,是两个 Protocol + 多实现的层:

| 接口 | 实现 | 在哪换 |
|------|------|--------|
| `Sandbox` | `SubprocessSandbox` / `ContainerSandbox` | `ProjectManager._sandbox_factory` 闭包(`projects/manager.py:280`) |
| `ContainerRuntime` | `DockerRuntime` / `OpenSandboxRuntime` / `FakeRuntime` | `ServerRuntimeContext._runtime`(`server/runtime_context.py:50`) |
| `Storage` | `FSStorage`(默认,可换) | `ProjectManager.storage_factory` |
| `Tool` | `FunctionTool` / `MCPWrapper` / 自定义 | `AgentLoop._build_tools` |

**为什么这样分?** 工具只关心「我能不能 read/write/execute」,不关心代码跑在
本地还是容器里;`ContainerSandbox` 只关心「把命令转给容器」,不关心是 Docker
还是 OpenSandbox。加一个新后端(比如未来的 K8s/gVisor)只需再实现
`ContainerRuntime`,上层全部不动。详见 [03 — 三层沙箱](03-sandbox.md)。

### Tenant-scoped 目录布局

这是多租户隔离的物理基础(`mini_cc/projects/layout.py:1`):

```
<data_dir>/
  tenants/
    <tenant_id>/
      projects/
        <project_id>/
          workspace/      ← sandbox 的 project_root(模型在这里干活)
          .state/         ← FSStorage 根,放 messages/todos/sessions/...
          meta.json       ← ProjectMeta(含 tenant_id)
      .storage/           ← 租户级 storage 根(每个 project 一个子目录)
  keys.json               ← 租户 API key registry
```

两个租户用**同一个 `project_id`** 会得到完全隔离的 workspace + storage。
跨租户访问一律 404(不是 403),这样不会泄漏别的租户项目是否存在。

### Project 装配

`ProjectManager._assemble()`(`mini_cc/projects/manager.py:278`)把一个项目
的所有依赖一次性 wire 起来。摘一段真实的装配代码:

```python
# mini_cc/projects/manager.py:278
def _assemble(self, project_id, meta):
    ws = workspace_path(self.root, meta.tenant_id, project_id)
    sandbox = self._sandbox_factory(meta.tenant_id, project_id, ws, self.policy)
    storage = self.storage_factory(self._state_root(meta.tenant_id))
    ...
    project = Project(project_id=project_id, workspace=ws, meta=meta,
                      sandbox=sandbox, storage=storage, ...)
    project.teams = TeammateSpawner(
        workspace=ws,
        loop_factory=lambda sid, _p=project: _build_teammate_loop(_p, sid),
        project_id=project_id, storage=storage)
    _connect_configured_mcp_servers(mcp_pool, ...)  # 三层 .mini_cc/ 发现
    return project
```

`as_ref()` 把 `Project` 转成精简的 `ProjectRef` 给 `AgentLoop` 用——解耦循环
与装配细节。Teammate 通过同一个 `_sandbox_factory` 闭包派生子 AgentLoop,这样
**容器化的租户会自动把容器沙箱传给 teammate**。

---

## 操作与配置

### 环境变量(常用)

| 变量 | 默认 | 用途 |
|------|------|------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `ANTHROPIC_BASE_URL` | — | 备用 base URL(代理、Anthropic 兼容服务) |
| `MODEL_ID` | `claude-sonnet-4-6` | 主模型 |
| `LITELLM_API_KEY` | — | OpenAI 兼容端点的 key(路由 `openai/*`、`deepseek/*` 等前缀模型) |
| `MINI_CC_DATA_DIR` | `./mini_cc_data` | 项目、状态、key registry 的根目录 |
| `MINI_CC_HOST` / `MINI_CC_PORT` | `127.0.0.1` / `8000` | 服务绑定 |
| `MINI_CC_SANDBOX_BACKEND` | `auto` | `auto` / `opensandbox` / `docker`,见 [03](03-sandbox.md) |
| `MINI_CC_SANDBOX_DEFAULT` | `subprocess` | 单租户默认沙箱: `subprocess` / `container` |

完整变量表见 `mini_cc/README.zh.md` 的「配置」节。`AnthropicConfig.has_llm_credentials()`
返回当前凭证是否就绪——`/health` 用它,避免误报 ok。

### CLI

```bash
python -m mini_cc.server                          # 默认 = serve
python -m mini_cc.server keygen <tenant>          # → mck_<32hex>
python -m mini_cc.server keys list <tenant>
python -m mini_cc.server keys rotate <key> [--grace-hours N]
python -m mini_cc.server revoke <key>
python -m mini_cc.server sandbox status [--tid T]
python -m mini_cc.server sandbox stop <tid>
python -m mini_cc.server sandbox build-image [--tag T] [--dockerfile P]
```

---

## 验证步骤

以下命令假定后端跑在 `127.0.0.1:8002`(README 里前端默认 base)。先启服务:

```bash
# 终端 1
python -m mini_cc.server keygen my_tenant    # 输出 mck_<hex>,记为 $KEY
MINI_CC_DATA_DIR=$PWD/mini_cc_data \
ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
ANTHROPIC_API_KEY=any-fake-key \
python -m mini_cc.server
```

```bash
# 终端 2 —— 验证分层链路
# 1. 存活探针(无鉴权)
curl -s http://127.0.0.1:8002/health
# → {"ok": true, ...}

# 2. 建项目(传 tenant_id 必须和 key 反解一致)
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"project_id":"demo","display_name":"Demo"}'

# 3. 开会话
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/demo/sessions \
     -H "Authorization: Bearer $KEY" -d '{"session_id":"s1"}'

# 4. 发一轮 —— SSE 流,看 text/tool_use/tool_result/done
curl -N -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/demo/sessions/s1/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"list the files here"}'

# 5. 跨租户隔离:用 my_tenant 的 key 访问 other_tenant → 404 不是 403
curl -s -o /dev/null -w "%{http_code}\n" \
     http://127.0.0.1:8002/tenants/other_tenant/projects/demo \
     -H "Authorization: Bearer $KEY"
# → 404
```

```bash
# 验证装配缓存 + 路由共享同一 Project 对象
python -m pytest tests/test_p3_projects.py -q   # 见 ProjectManager.get 缓存测试
```

---

## 常见坑与调试

1. **SDK 直接用首条 send 抛 LLM 401。** SDK 不做隐式凭证发现,显式
   `set_default_config(AnthropicConfig(api_key=...))` 是最稳的。审计 P0-5 指出的
   最大 SDK 痛点。诊断:`AnthropicConfig.has_llm_credentials()` 返回 False 就是
   凭证没配。
2. **`project_busy` 409 频繁出现。** 同一项目并发 send 串行化是设计;第二个并发
   立即 409 而非排队。如果你在客户端串行重试,确认上一个 send 真的收到 `done`
   或客户端断开(否则 server 端的 RLock 还没释放)。
3. **改了 `.mini_cc/.mcp.json` 没生效。** 装配缓存按 mtime 失效
   (`_config_signature`),但 `create()` 故意不 prime 缓存。手动
   `pm.invalidate(pid, tenant_id=tid)` 强制下次 `get()` 重装。
4. **Windows 上 `mkdir -p` 建出叫 `-p` 的目录。** `SubprocessSandbox` 会优先找
   bash;没有 bash 才回落到平台默认 shell。装 Git Bash 或 WSL 解决。
5. **认为沙箱是硬安全边界。** `Policy` 是 defense-in-depth 一层,挡不住有决心
   的对手。不受信代码请开容器沙箱(见 [03](03-sandbox.md))。

---

## 延伸阅读

- 兄弟章:[02 — 存储、项目与会话](02-storage-projects-sessions.md) ·
  [03 — 三层沙箱](03-sandbox.md)
- 概念章:[s01 — The Agent Loop](../../zh/s01-the-agent-loop.md) ·
  [s02 — Tool Use](../../zh/s02-tool-use.md)
- 源码:`mini_cc/README.zh.md`(子系统地图)、`mini_cc/ARCH.zh.md`(完整 Mermaid)
- 进阶:`docs/mini_cc/container-sandbox.md`(P5 容器沙箱)、
  `docs/zh/2026-06-28-functional-features.zh.md`(F1-F7 功能手册)
