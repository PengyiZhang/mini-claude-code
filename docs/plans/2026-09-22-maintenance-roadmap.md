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

## M1. 稳定性基础（工程卫生）— 建议立即执行 ✅ 2026-09-22 完成

目标：让"跑测试"与"装依赖"两件事在任何机器上都成立。

- [x] **M1-1 建立 CI**（新仓库目前零 workflow）：GitHub Actions，矩阵
  `ubuntu + windows` / `python 3.11+3.12`，`pip install -r requirements.txt && pytest tests/ -q --ignore=tests/test_teammate_real_llm.py`。前端可先只跑 `tsc --noEmit`。
- [x] **M1-2 补齐未声明依赖**：`portalocker`（`teams/__init__.py:37` 有守卫但默认安装
  会**静默降级为无文件锁**）与 `requests`（`channels/feishu.py:53`、
  `sharing/webhooks.py:34`）加入 requirements.txt + pyproject.toml；或改用已依赖的 httpx。
- [x] **M1-3 修复 flaky 测试**：`test_loop_nudge.py` 5 处 `time.sleep` 改事件/条件等待；
  顺带盘点 tests/ 其余 85 处 sleep 的高危子集。
  （实际根因：最后一项断言在"错误事件已发、`_running` 未清"窗口内急切断言——
  `_run_impl` 在 try 内 emit、finally 才清标志。已改为 deadline 等待；50 次循环 0 失败，
  修复前约 1/3 失败。其余 sleep 均已是 deadline 轮询模式，无需改。）
- [x] **M1-4 修正 mini_cc/README.md 陈旧声明**：`289 tests`→以 CI 徽章替代硬数字；
  `:289/:650 "P5 work, not yet shipped"`→opensandbox 已发布；
  `:573 8002 端口轶事`→改为参数化说明（默认 8000，见 `server/cli.py:119`）。
- [x] **M1-5 版本单一事实源**：`pyproject.toml` 与 `mini_cc/__init__.py:55` 双写 0.1.0，
  改为 `importlib.metadata` 读取或同步脚本；建立 git tag + CHANGELOG.md 惯例。

## M2. 安全加固（上线阻塞项）— 来自 production-hardening，逐项复核均未修 ✅ 2026-09-23 完成

目标：堵住跨租户/宿主机面。

- [x] **M2-1 S2 shell 注入面**：`sandbox/subprocess_sandbox.py:239` cmd.exe `shell=True`
  回退路径 + `sandbox/policy.py` 缺 `\n`/`$(`/反引号 拒绝。
  （实现：`Policy.scan_command` 增加不可关闭的结构性规则 `newline_in_command` /
  `command_substitution`；无 POSIX shell 时 `execute()` 抛 `no_posix_shell` 拒绝执行，
  不再回退 cmd.exe。测试 `tests/test_m2_sandbox_policy.py`。）
- [x] **M2-2 S5 API key 明文**：`auth/keys.py:112` 磁盘明文存储；改 sha256 存储 +
  `hmac.compare_digest` 校验（保留 key 前缀明文便于识别）。
  （实现：keys.json 以 `sha256:<hex>` 为键存储，记录含非敏感 `key_hint` 前缀；
  旧明文条目经 `_find_name` 回退继续认证、下次写入时自动再哈希；admin 路由改用
  新增的 `registry.find()`。测试 `tests/test_auth_key_hashing.py`。）
- [x] **M2-3 S6 extra_mounts 无白名单**：`sandbox/config.py:157` 任意宿主路径可挂载；
  加 `MINI_CC_EXTRA_MOUNTS_ALLOW` 白名单校验。
  （实现：敏感宿主前缀（/etc /root /home /var/lib/docker /proc /sys /dev /boot /run
  + data_dir）恒拒；env 配置后为严格白名单模式，且白名单不能豁免拒绝前缀。
  测试 `tests/test_m2_mount_whitelist.py`。）
- [x] **M2-4 S1 路径 TOCTOU**：`sandbox/subprocess_sandbox.py:109` 仅 resolve 检查，
  换 fd-based open（`os.open` + `O_NOFOLLOW` 等价物）。
  （实现：read/write/edit 走 `_open_validated()`——校验解析路径 → `os.open` 拿 fd →
  fstat 与 nofollow-stat 比对 inode/设备，最终组件成 symlink 或身份漂移即抛
  `PathEscapeError`。symlink 竞态用例由 Linux CI 执行（本机无 symlink 权限）。
  测试 `tests/test_m2_toctou.py`。）
- [x] **M2-5 S3 框架层租户守卫**：`projects/manager.py:233` `tenant_id=None` 全局扫描
  仍是隐患 API，改为显式 require-tenant 或审计日志。
  （实现：`get/list/delete` 缺 tid 一律 ValueError；跨租户场景显式走新增的
  `get_any()/list_all()`（migrate、渠道索引、分享 token 解析等内部工具）；
  顺带修复 app.py 关机清理引用不存在的 `pm._projects` 导致队友/MCP 清理从未执行的
  死代码 bug。测试 `tests/test_m2_tenant_guard.py`。）
- [x] **M2-6 渠道 webhook 加固**：`routes/channels.py:179` 无鉴权无节流，飞书签名
  校验依赖可选 encrypt_key（`feishu.py:189`）→ 未配置即可伪造消息触发**付费 LLM 调用**；
  要求每渠道强制 secret + 复用 rate limiter + `_find_binding` 线性扫描加索引。
  （实现：飞书渠道新增 `verification_configured`（encrypt_key 或 verification_token），
  入站路由对无校验材料的渠道返回 401；`MINI_CC_CHANNEL_RPM`（默认 30/min）按渠道
  限流 + 429/Retry-After；`app.state.channel_index` 缓存渠道→项目映射（create/delete
  维护，miss 时单次扫描回填）。顺带根治飞书 kind 只在 lifespan 注册导致的测试
  顺序依赖（移入 build_app）。测试 `tests/test_m2_channel_webhook.py`。）
- [x] **M2-7 R8 轮换宽限期降权**：`auth/keys.py:187` 宽限期旧 key 保留全部 scope，
  应降为只读。
  （实现：`_read_only_scopes()` 把宽限期旧 key 的 scope 投影为只读视图
  （`*`→`read:*`、`sessions:*`→`sessions:read`、write-only 丢弃），绝不放宽；
  重启重载后保持。测试见 `tests/test_auth_key_hashing.py` 后三用例。）
- [x] **M2-8 /shared/{token} 限流**（公开可爆破面，token 为 HMAC 签名但无速率限制）。
  （实现：`share_rate_limit` 依赖按客户端 IP 分桶，`MINI_CC_SHARE_RPM`（默认 60/min）
  可调；429 + Retry-After 走标准 envelope。测试 `tests/test_m2_share_ratelimit.py`。）

## M3. 可维护性重构 — 结构债 🔶 2026-09-23 部分完成（M3-2 待做）

目标：让下一个贡献者能读懂。

- [x] **M3-1 拆分 `commands/registry.py`（2313 行/45 defs）**：按命令族拆模块 +
  装饰器注册；拆完跑全量测试守护行为。
  （实现：registry.py 2313→128 行，仅保留 CommandContext/SlashCommand/
  CommandRegistry 核心类型 + default_registry() 聚合器；处理器按 8 个命令族迁入
  `commands/builtin/`（session/skills/project/agents/sched/config/workflow + _util
  共享助手），各带 `register(reg)`；测试的私有函数导入改指新模块。命令电池 245
  测试全绿。）
- [ ] **M3-2 拆分 `teams/__init__.py`（1304 行）**：MessageBus / ProtocolTracker /
  TeammateSpawner / mailbox 各自成模块。
  （**待做**——与 registry 拆分同规模但耦合更紧（Spawner 依赖 Bus+Tracker），
  留作独立专注批次，勿与其它改动混提交。）
- [x] **M3-3 except 卫生**：192 处 `except Exception` 分级处理；最高优先
  `routes/channels.py:240` 渠道 worker 整轮吞错——至少补 warning 日志。
  （实现：渠道 worker 失败/无会话路由两条路径均落 warning 日志，带 project/
  session 上下文与 exc_info traceback（test_m3_except_hygiene.py caplog 验证）。
  其余 190 处按"分级处理"原则逐步消化——留待日常触碰时顺手改，不再批量扫。）
- [x] **M3-4 print → logging**：`server/cli.py`、`tools/repl.py`、`projects/migrate_*.py`
  等 37 处。
  （**按策略关闭**：stdout 输出是 CLI/迁移脚本/REPL 的用户界面，保留 print；
  repl.py 的两处 stderr 诊断实为**子进程生成代码模板**内的 print（子进程无
  logging），保持原样是正确做法。真正的诊断类日志（如渠道 worker）已在 M3-3
  落 logging。剩余 print 均为界面输出，不转换。）
- [x] **M3-5 线程关闭闭环**：watcher / MCP reader / channel worker 均为 daemon 且从不
  join（仅 `teams/__init__.py:794`、`tools/background.py:187` 两处 join）；补优雅停机
  路径与测试。
  （实现：channel-inbound worker 线程登记入 `_INBOUND_WORKERS`（锁保护，
  完成即自剔除）；`drain_inbound_workers(timeout)` 有界 join 超时未完线程保留
  追踪；接入 lifespan 停机（teammates/MCP 之后、容器关停之前）。watcher/MCP/
  teammates 的停机路径在 M2-5 修复死代码后本已生效（spawner.shutdown 会 join
  worker、stop_lead_watcher、mcp_pool.disconnect_all）。测试见
  test_m3_except_hygiene.py 后两用例。）
- [x] **M3-6 `_shim.py` 退役计划**：文档标注 deprecated，n 个版本后移除。
  （实现：docstring 标注 deprecated 0.2 / remove in 0.3；调用即发
  DeprecationWarning。）

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
| 2026-09-22 | M1 全部 | M1-hygiene commit（2026-09-22） | CI workflow（pytest 双平台矩阵 + web build/test）+ CI 徽章；portalocker/requests 入依赖清单（mailbox 15 测试转绿）；nudge flaky 断言修复（50 次循环验证，1343 全绿）；README 三处陈旧声明修正；版本单一源 + CHANGELOG.md + v0.1.0 tag |
| 2026-09-23 | M2 全部 | `0b28238` | 8 项全落地（TDD，各配 PoC 测试）：结构性元字符拒绝 + 拒绝 cmd.exe 回退；key sha256 落盘 + key_hint + 旧明文自动迁移；mounts 敏感前缀恒拒 + 可选严格白名单；fs 操作 fd 化 + 身份比对；get/list/delete 强制 tid + 显式 get_any/list_all（并修复 app.py 死代码关机清理）；渠道 webhook 强制校验材料 + 每渠道限流 + 索引（顺带根治 feishu 注册顺序依赖）；宽限期旧 key 只读投影；/shared 按客户端 IP 限流。CI 双平台 5/5 绿（Linux 实跑 symlink TOCTOU 用例） |
| 2026-09-23 | M3 除 M3-2 | (本次) | registry.py 2313→128 行拆 8 命令族模块（245 命令测试绿）；渠道 worker 吞错补 warning+traceback（caplog TDD）；channel worker 登记与 lifespan 有界 drain；_shim 弃用告警；M3-4 按策略关闭（stdout=界面）。M3-2 teams 拆分留独立批次 |
