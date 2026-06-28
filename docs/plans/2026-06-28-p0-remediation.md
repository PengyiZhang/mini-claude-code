# P0 Remediation — 三路深度 Audit 后的修复路线图

> **For Claude:** 本文档是 audit 结论的固化 + 分批执行计划。Batch 1 已拆为可执行小步；Batch 2-5 待执行前再细化。

**生成时间:** 2026-06-28
**当前分支:** `dev-functional`（846 tests passing）
**审计来源:** 3 个 code-reviewer agent 并行执行
  - Agent 执行核心层（loop / recovery / compaction / subagent）
  - 传输与会话层（HTTP/SSE / session 生命周期 / 鉴权 / 存储）
  - 易用性与开发者体验（端到端流程 / 错误反馈 / 命令 UX / 文档）

与既有 `docs/plans/2026-06-28-production-hardening.md` 的关系：那篇是更早的全局生产化评估，本文档聚焦本轮三路 audit 新发现的 P0/P1，互为补充。

---

## 0. 优先级矩阵（合并去重后）

按"风险严重度 × 触发频率 × 修复成本"排序。

### P0 — 必须修

| ID | 问题 | 文件:行 | 来源 |
|----|------|---------|------|
| **P0-1** | Webhook 路由跨租户越权 | `server/routes/webhooks.py:27-33` | TS |
| **P0-2** | 客户端取消后 AgentLoop 继续烧 token | `core/loop.py:429-467`, `server/sse.py:68-76` | TS+AL |
| **P0-3** | LLM 凭证缺失启动静默成功 | `config.py:108-122`, `server/cli.py:56-131`, `server/app.py:123-125` | UX |
| **P0-4** | SSE `error` 事件与 HTTP 错误信封不兼容 | `server/sse.py:45`, `server/routes/sessions.py:185` | UX+TS |
| **P0-5** | SDK 入口文档缺 LLM 配置步骤 | `mini_cc/__init__.py:6-13`, `README.zh.md:106-123` | UX |
| **P0-6** | 子 agent transcript 永不落盘 | `core/subagent.py:67-82`, `teams/__init__.py:250-275` | AL |
| **P0-7** | teammate 递归深度无上限 | `core/subagent.py:23-25`, `tools/teams.py:67-84` | AL |
| **P0-8** | SSE 无心跳 + 无重连 | `server/sse.py:20-77`, `web/src/lib/sse.ts:27-97` | TS |
| **P0-9** | 流式取消后 tool_use 在历史里完全丢失 | `core/loop.py:429-467` | AL |
| **P0-A** | Webhook SSRF（允许 169.254.169.254 / localhost） | `sharing/webhooks.py:102-104` | 上轮 audit |
| **P0-B** | Embed iframe 全局可嵌（clickjack） | `server/routes/sessions.py:259` | 上轮 audit |
| **P0-C** | 默认 dev secret 启动时无告警 | `sharing/tokens.py:86-93` | 上轮 audit |
| **P0-D** | webhook 与 share token 共用同一密钥 | `sharing/webhooks.py:42-50` | 上轮 audit |

### P1 — 强烈建议（精选）

| ID | 问题 | 文件:行 |
|----|------|---------|
| P1-1 | `save_todos`/`save_task`/`save_cron`/`write_tool_result` 不走原子写 | `storage/fs.py:263-322` |
| P1-2 | 流式 `text_delta` 后异常：客户端看到、磁盘没有 | `core/loop.py:428-450` |
| P1-3 | `compact_history` 切碎 use/result 配对 → API 400 | `core/compaction.py:108-126` |
| P1-4 | 写操作无幂等键 → 重试双扣费 | `server/schemas.py:25-26` |
| P1-5 | 流式中断 UI 不给"重试"按钮 | `web/src/lib/sse.ts:90-96` |
| P1-6 | 工具错误信息普遍不可操作 | `tools/{fs,bash,web,websearch}.py` |
| P1-7 | `try_lock` 与 `send` 之间 TOCTOU → 409 失效 | `server/routes/sessions.py:176-182` |
| P1-8 | 项目级锁粒度过粗 → 同项目多 session 串行 | `session/manager.py:188-190` |
| P1-9 | `repair_dangling_tool_uses` 单向检查 | `core/loop.py:93-129` |
| P1-10 | `/health` 假性健康（与 P0-3 同因） | `server/app.py:123-125` |
| P1-11 | `_ensure_warm` 与 `start_session`/`remove` 之间 race | `session/manager.py:64-95` |

---

## 1. Batch 1：安全 + 首跑（目标：堵住越权 + 让新用户首跑成功）

**Scope：** P0-1, P0-3, P0-5, P0-A, P0-B, P0-C, P0-D（共 7 项，全是小到中等修复）
**预计：** 半天～1 天
**验收：** 846 + N tests passing；新增的失败攻击场景都被对应测试覆盖。

### Task 1.1 — Webhook 跨租户越权（P0-1）

**Files:** `mini_cc/server/routes/webhooks.py`, `tests/test_functional_webhooks.py`

**Step 1:** 写失败测试
```python
def test_webhook_routes_reject_cross_tenant(tmp_path, ...):
    # tenant A's key tries to read tenant B's project webhooks → 404
```

**Step 2:** 验证测试失败（404 vs 当前 200）

**Step 3:** 在 `_registry_for` 顶部加 tenant 校验，与 `sessions.py`/`commands.py` 用同一个 `_check_project_tenant` helper

**Step 4:** 测试通过 + 全量回归

**Step 5:** commit

### Task 1.2 — Webhook SSRF 防护（P0-A）

**Files:** `mini_cc/sharing/webhooks.py`, `tests/test_functional_webhooks.py`

**Step 1:** 写失败测试：`add("http://169.254.169.254/", [])` / `add("http://localhost:9000", [])` / `add("http://10.0.0.1", [])` 都 raise ValueError

**Step 2:** 实现 `_validate_webhook_url`：用 `urllib.parse.urlparse` + `socket.getaddrinfo` 解析主机，拒绝 `ipaddress.ip_address(...).is_private / is_loopback / is_link_local / is_reserved`；同时设 `allow_redirects=False`

**Step 3:** 测试通过

**Step 4:** commit

### Task 1.3 — Embed iframe clickjack（P0-B）

**Files:** `mini_cc/server/routes/sessions.py`

**Decision:** 不绑 origin（embed 通常跨域），改用 CSP `frame-ancestors *` 显式声明（让浏览器知道是公开嵌入）+ 在 embed HTML 顶部加可见 watermark "Shared (read-only)" + JS disable。同时移除全局 `X-Frame-Options: ALLOWALL`（或保留但加上 CSP frame-ancestors，CSP 优先级更高）。

**Step 1:** 写失败测试：embed 页面响应头含 `Content-Security-Policy: frame-ancestors ...`，HTML 含 watermark 元素

**Step 2:** 修改 `_EMBED_HTML` 模板加水印；在 `shared_embed` 路由 response 设 CSP header

**Step 3:** commit

### Task 1.4 — Dev secret 启动告警（P0-C）

**Files:** `mini_cc/sharing/tokens.py`, `mini_cc/sharing/webhooks.py`, `mini_cc/server/app.py`（或 cli.py）

**Step 1:** 在 `tokens.py` 暴露 `is_using_dev_secret() -> bool`；`webhooks.py` 同样

**Step 2:** 在 FastAPI startup hook（`server/app.py` 的 lifespan 或 cli.py 的 `cmd_serve`）调两个函数，若 True 则 `logging.getLogger(__name__).warning(...)` 醒目告警

**Step 3:** 测试：临时 unset secret 启动 → capture logs → 断言含 "DEV FALLBACK"

**Step 4:** commit

### Task 1.5 — Webhook / Share 密钥分离（P0-D）

**Files:** `mini_cc/sharing/webhooks.py`, `tests/test_functional_webhooks.py`

**Step 1:** 改 `_webhook_secret`：去除 `MINI_CC_SHARE_SECRET` 回退；未设 `MINI_CC_WEBHOOK_SECRET` 时**报错**（而不是回退到 share secret）

**Step 2:** 测试：未设 webhook secret 时调用 sign → 折回 dev fallback（与 P0-C 配合，启动时已告警）但**不再与 share secret 共用**

**Step 3:** commit

### Task 1.6 — LLM 凭证 fail-fast + /health 真实化（P0-3）

**Files:** `mini_cc/server/cli.py`, `mini_cc/server/app.py`, `mini_cc/config.py`, `tests/test_p0_server.py`

**Step 1:** 写失败测试：`cmd_serve` 启动时若 `cfg.api_key is None` 且 provider 是 anthropic → 抛 `SystemExit` 含操作指引

**Step 2:** 在 `cmd_serve` 的 lifespan startup 调 `cfg.build_provider()` 的最小校验（key 非空、base_url 可达可选）

**Step 3:** `/health` 增加 `llm_provider: "configured"|"unconfigured"` 字段

**Step 4:** commit

### Task 1.7 — SDK 入口文档补 LLM 配置（P0-5）

**Files:** `mini_cc/__init__.py`, `mini_cc/README.zh.md`

**Step 1:** `__init__.py` docstring 补三段示例（含 `set_default_config(AnthropicConfig(api_key=...))`）

**Step 2:** README SDK 段对齐

**Step 3:** commit

---

## 2. Batch 2：取消路径 + 子 agent 连续性（执行前再细化）

**Scope：** P0-2, P0-6, P0-7, P0-9, P1-2, P1-9
**主线：** "中断时的状态一致性" —— 客户端取消、provider 异常、子 agent 崩溃都要保证 transcript 可恢复、token 不浪费。

预估 1-2 天。

## 3. Batch 3：错误反馈统一 + UI 重试（执行前再细化）

**Scope：** P0-4, P1-5, P1-6, P1-10
**主线：** 把错误从"技术栈倾倒"升级到"可操作提示"。

预估半天。

## 4. Batch 4：存储原子性 + 配对修复（执行前再细化）

**Scope：** P1-1, P1-3, P1-11
**主线：** 半截 JSON / 切碎 use-result / 并发 dict race。

预估半天。

## 5. Batch 5：SSE 重连（独立迭代）

**Scope：** P0-8, P1-7, P1-8
**主线：** 事件 ID + 环形缓冲 + 前端重连 + per-session 锁。

预估 2-3 天，单独立项。

---

## 执行规则

- **每个 Task 内遵循 TDD：先写 failing test → minimal impl → 全量回归 → commit。**
- **commit message 格式：** `fix(<area>): <P0-ID> <one-line>`，body 引用 audit 来源。
- **跨 Task 不批量 amend**，每个 Task 一个独立 commit，便于回滚。
- **Batch 1 全部完成后跑一次全量 + 手动 smoke**，再开 Batch 2 plan。
