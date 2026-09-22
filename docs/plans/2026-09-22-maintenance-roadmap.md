# mini_cc 长期维护路线图（Living Roadmap）

> **用途**：mini_cc 的可跟踪升级维护蓝图。每个里程碑带复选框，完成即勾选并在
> "完成记录"补一行证据（commit/测试）。审计基线：2026-09-22，main @ b2c5f0d。
>
> **上游审计来源**（2026-06-28 系列，本文已逐项核对完成状态）：
> `functional-roadmap.md` / `production-hardening.md` / `production-audit-round2.md`
> / `p0-remediation.md`。本文取代它们作为唯一的 active 跟踪文档。

---

## 0. 状态快照（2026-09-22）

| 维度 | 状态 |
|---|---|
| 测试 | 1341 通过 / 2 失败（1 个 wall-clock flaky；1 个因 portalocker 未安装——见 M1-2）/ 24 subtests，128 个文件 |
| 功能面 | Agent 内核、teams、workflow V1+V2、channels(飞书)、sharing、三层插件/记忆、LSP、模板、卡片命令、Web 控制台全部落地 |
| 旧 P0 安全修复 | p0-remediation Batch1 与 round2 Batch3/4/6/7 已完成并各有测试 |
| 安全残留 | production-hardening S1/S2/S3/S5/S6 五项仍未修（见 M2） |
| CI | **无**（原上游 CI 已随 reference/ 归档，新仓库零 workflow） |
| 已知结构债 | registry.py 2313 行、teams/__init__ 1304 行、192 处宽泛 except |
| 文档 | 根 README 已对齐实现；mini_cc/README.md 有 3 处陈旧声明（见 M1-4） |

---

## M1. 稳定性基础（工程卫生）— 建议立即执行

目标：让"跑测试"与"装依赖"两件事在任何机器上都成立。

- [ ] **M1-1 建立 CI**（新仓库目前零 workflow）：GitHub Actions，矩阵
  `ubuntu + windows` / `python 3.11+3.12`，`pip install -r requirements.txt && pytest tests/ -q --ignore=tests/test_teammate_real_llm.py`。前端可先只跑 `tsc --noEmit`。
- [ ] **M1-2 补齐未声明依赖**：`portalocker`（`teams/__init__.py:37` 有守卫但默认安装
  会**静默降级为无文件锁**）与 `requests`（`channels/feishu.py:53`、
  `sharing/webhooks.py:34`）加入 requirements.txt + pyproject.toml；或改用已依赖的 httpx。
- [ ] **M1-3 修复 flaky 测试**：`test_loop_nudge.py` 5 处 `time.sleep` 改事件/条件等待；
  顺带盘点 tests/ 其余 85 处 sleep 的高危子集。
- [ ] **M1-4 修正 mini_cc/README.md 陈旧声明**：`289 tests`→以 CI 徽章替代硬数字；
  `:289/:650 "P5 work, not yet shipped"`→opensandbox 已发布；
  `:573 8002 端口轶事`→改为参数化说明（默认 8000，见 `server/cli.py:119`）。
- [ ] **M1-5 版本单一事实源**：`pyproject.toml` 与 `mini_cc/__init__.py:55` 双写 0.1.0，
  改为 `importlib.metadata` 读取或同步脚本；建立 git tag + CHANGELOG.md 惯例。

## M2. 安全加固（上线阻塞项）— 来自 production-hardening，逐项复核均未修

目标：堵住跨租户/宿主机面。

- [ ] **M2-1 S2 shell 注入面**：`sandbox/subprocess_sandbox.py:239` cmd.exe `shell=True`
  回退路径 + `sandbox/policy.py` 缺 `\n`/`$(`/反引号 拒绝。
- [ ] **M2-2 S5 API key 明文**：`auth/keys.py:112` 磁盘明文存储；改 sha256 存储 +
  `hmac.compare_digest` 校验（保留 key 前缀明文便于识别）。
- [ ] **M2-3 S6 extra_mounts 无白名单**：`sandbox/config.py:157` 任意宿主路径可挂载；
  加 `MINI_CC_EXTRA_MOUNTS_ALLOW` 白名单校验。
- [ ] **M2-4 S1 路径 TOCTOU**：`sandbox/subprocess_sandbox.py:109` 仅 resolve 检查，
  换 fd-based open（`os.open` + `O_NOFOLLOW` 等价物）。
- [ ] **M2-5 S3 框架层租户守卫**：`projects/manager.py:233` `tenant_id=None` 全局扫描
  仍是隐患 API，改为显式 require-tenant 或审计日志。
- [ ] **M2-6 渠道 webhook 加固**：`routes/channels.py:179` 无鉴权无节流，飞书签名
  校验依赖可选 encrypt_key（`feishu.py:189`）→ 未配置即可伪造消息触发**付费 LLM 调用**；
  要求每渠道强制 secret + 复用 rate limiter + `_find_binding` 线性扫描加索引。
- [ ] **M2-7 R8 轮换宽限期降权**：`auth/keys.py:187` 宽限期旧 key 保留全部 scope，
  应降为只读。
- [ ] **M2-8 /shared/{token} 限流**（公开可爆破面，token 为 HMAC 签名但无速率限制）。

## M3. 可维护性重构 — 结构债

目标：让下一个贡献者能读懂。

- [ ] **M3-1 拆分 `commands/registry.py`（2313 行/45 defs）**：按命令族拆模块 +
  装饰器注册；拆完跑全量测试守护行为。
- [ ] **M3-2 拆分 `teams/__init__.py`（1304 行）**：MessageBus / ProtocolTracker /
  TeammateSpawner / mailbox 各自成模块。
- [ ] **M3-3 except 卫生**：192 处 `except Exception` 分级处理；最高优先
  `routes/channels.py:240` 渠道 worker 整轮吞错——至少补 warning 日志。
- [ ] **M3-4 print → logging**：`server/cli.py`、`tools/repl.py`、`projects/migrate_*.py`
  等 37 处。
- [ ] **M3-5 线程关闭闭环**：watcher / MCP reader / channel worker 均为 daemon 且从不
  join（仅 `teams/__init__.py:794`、`tools/background.py:187` 两处 join）；补优雅停机
  路径与测试。
- [ ] **M3-6 `_shim.py` 退役计划**：文档标注 deprecated，n 个版本后移除。

## M4. 可靠性与性能 — 对外承诺 SLA 前完成

- [ ] **M4-1 R2 错误状态机**：`core/loop.py` 错误目前直接进 transcript，缺
  ErrorBlock/瞬态标记。
- [ ] **M4-2 R4 重试分类**：`core/recovery.py:35` 仅处理 429/529；
  quota/invalid_key 类不可重试错误应快速失败。
- [ ] **M4-3 R6 /readyz**：storage/docker 探针（/health 已含 llm_configured）。
- [ ] **M4-4 R5 指标补全**：tool 失败率、MCP 状态、锁等待等计数器。
- [ ] **M4-5 X3 长会话加载**：`storage/fs.py` O(n) 全量读，加分块/窗口。
- [ ] **M4-6 X4 阻塞 IO 出 event loop**：除 `server/sse.py:130` 外几乎全同步；
  渐进式 aiofiles/asyncio.subprocess。
- [ ] **M4-7 B1 并行工具执行**：`core/loop.py` 串行执行工具（round2 Batch5 遗留）。
- [ ] **M4-8 X1 无状态化**：目前仅 ratelimit 有 Redis 后端；warm-loop/SSE pub-sub 待做。

## M5. 功能演进 — 来自 functional-roadmap 尚未完成项

- [ ] **M5-1 F2 多模态**：聊天图片附件 → vision 模型（上传管线已有，缺 b64/图像块
  注入 `core/loop.py`）；`doc_parse` 工具（PDF/Word/Excel → markdown）。
- [ ] **M5-2 F3 余项**：会话标签（tag）与按标签过滤。
- [ ] **M5-3 F5 余项**：子代理角色模板、并行任务数组语法。
- [ ] **M5-4 F6 余项**：场景化模板与组合命令（/code-review 等复合命令包）。
- [ ] **M5-5 F8 DX**：`mini-cc run` 一次性执行子命令、SDK 文档站/Playground。

---

## 长期维护原则

1. **本文档是唯一 active 跟踪面**；旧 roadmap 文档转为历史参考，不再更新。
2. 每完成一项：勾选复选框 + 在下方"完成记录"追加一行 `日期 · 项号 · commit · 一句话`。
3. 每季度跑一次全面审计（可复用本次的 Explore 双 agent 方法：旧项核对 + 新鲜眼审计），
   刷新状态快照。
4. README 中的数字类声明（测试数等）一律改为 CI 徽章或动态来源，禁止硬编码。
5. 新功能合入前必须带测试；安全类修复参照 M2 逐项附 PoC 测试。

## 完成记录

| 日期 | 项 | commit | 说明 |
|---|---|---|---|
| 2026-09-22 | 预备 | (本次) | 回收误入 reference/ 的 3 份用户文档（deploy-runbook 等），修复 DEPLOYMENT.md 断链 |
