[ < [14](14-web-ui-observability-deploy.md) ] · [English version](../en/15-evolution-migration.md)

# 15 — 从教学框架到生产级框架的演进

> 前十四章拆了 mini_cc *今天的形态*。这一章讲它*怎么变成这样的*——一份按 git 时间线
> 还原的演进史。mini_cc 不是某天写出来的成品,而是从 `c6a27ef` 的 11 节教学仓库起步,
> 经过 P0-P6 基础设施、Phase A-F 服务端加固、Phase G-J 能力扩展、round-2 生产审计、
> Workflow V2(W1-W6 + B8)逐步长成。每一步都有"为什么需要它"和"它打破了什么"。这
> 一章把散落在 60+ 提交里的演进逻辑串成一条线,告诉你哪些决策是路径依赖、哪些是设计
> 不变量、以及为什么"能跑的教学代码"和"能上线的生产框架"之间隔着十道门。

---

## 起点:11 节教学会话 / Starting point: 11 progressive sessions

仓库起点是 `c6a27ef feat: build an AI agent from 0 to 1 -- 11 progressive sessions`。
这个提交建立了 `agents/` 目录——12 份单文件 Python 教学示例:`s01_agent_loop.py`(120 行)
讲最小 agent 循环,`s02_tool_use.py` 讲工具派发,一路到 `s11_autonomous_agents.py`(586 行)
讲自治团队,`s12_worktree_task_isolation.py` 讲 git worktree 隔离,外加 `s_full.py` 聚合
参考。每节只加一个机制——单文件、单租户、内存态。这套形态适合学概念:打开 `s01`,120 行
从头读到尾,没有抽象层、没有 Protocol、没有目录布局。但正是这种"没有"决定了它**活不到
生产**:

1. **全局状态。** `s11` 的 storage、scheduler、mcp_pool 是模块级单例。同进程跑两个用户,
   messages/tasks/cron 互相串,谁先 `cd` 谁改全局 cwd。
2. **同步阻塞函数。** `agent_loop()` 是返回 list 的同步函数,后端要用就得自己包传输、
   并发、鉴权——这层每个团队都在重复造。
3. **没有租户边界。** 所有状态落 cwd 下,跨用户隔离靠"约定不同目录",不是强制约束。

这一节的真正价值是把 19 个 Claude Code 子系统逐个讲清,产出一份"我们要保留哪些能力"
的清单。mini_cc 的全部目标一句话概括:**保留 `s_full` 的全部能力,但重构成多租户、可被
后端嵌入、无模块级全局状态的框架**。

---

## 第一步:P0+P1+P2 框架骨架 / Step 1: framework scaffolding

提交 `76647ac feat: scaffold mini_cc multi-tenant agent framework (P0+P1+P2)` 一次性落了
2255 行,把教学代码翻成框架骨架。三个决策定义了后续所有演进的边界:

**决策一:Storage 作为 Protocol。** `mini_cc/storage/base.py:51` 定义 `Storage(Protocol)`,
覆盖 messages/todos/tasks/memory/cron/sessions/search/workflow 共 19 个方法。默认实现
`FSStorage`(`storage/fs.py:48`)基于原子写(`_atomic_write_json` 在 `fs.py:146`)+
 per-key `threading.Lock`。把存储抽象成 Protocol 是后续所有"可换实现"决策的根:Workflow V2
加表项不动 SDK 核心,P0-3 修原子写不动上层,未来换 Redis/PG 只是再实现一个 Storage。关键
是**先有 Protocol 再有实现**,反过来每个调用点都会绑死具体类。

**决策二:projects / sessions 拆分。** `mini_cc/projects/manager.py:123` 的 `ProjectManager`
管租户元数据 + 目录布局 + 装配缓存;`mini_cc/session/manager.py` 的 `SessionManager` 管
"项目内多会话编排 + per-project RLock 串行"。这两层分开,意味着"装配项目"(连 MCP、建
调度器、绑沙箱,昂贵要缓存)与"跑一轮对话"(高频要走锁)是两件事。`ProjectManager._assemble()`
(`projects/manager.py:278`)一次性 wire 全部依赖,装配按 `.mcp.json`/`permissions.toml`
的 mtime 失效(`get()` 带缓存,`projects/manager.py:225`)。

**决策三:多租户目录布局。** `mini_cc/projects/layout.py` 定义 tenant-scoped 布局:
`<data_dir>/tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}`。两租户用同一个
`project_id` 得到完全隔离的 workspace + storage;跨租户访问一律 404(不是 403,不泄漏
项目是否存在)。这个物理布局是多租户隔离的根——后面 Phase D scope、round-2 R2 测试都
在这个根上长。

P0+P1+P2 不是"实现了一部分功能",而是**定下了框架的不变量**:Protocol-based 扩展点、
无模块级全局状态、租户隔离从 day 1 开始。后面九步全建立在这三个不变量上。

---

## 第二步:P3 HTTP/SSE 传输 / Step 2: HTTP/SSE transport

提交 `3b27fb7 feat(server): HTTP/SSE transport layer (P3)`(1406 行)是 mini_cc 第一次
穿过网络边界。`agent_loop()` 同步阻塞,但 HTTP 客户端要流式接收 token——需要桥。
`mini_cc/server/sse.py`(76 行)实现这个桥:worker 线程跑同步的 `SessionManager.send()`,
通过 `asyncio.Queue` 把 yield 出来的 event 喂给 ASGI 流式响应,按
`id: <seq>\ndata: <json>\n\n` 分帧。`mini_cc/server/routes/sessions.py`(112 行)暴露
`POST /send` 返回 `text/event-stream`。

传输层(`mini_cc/server/`)刻意对 SDK 核心零感知:`AgentLoop` 不知道自己被 HTTP 路由调用
还是被 SDK 直接调用。这解耦让 mini_cc 既能 `import mini_cc` 当 SDK,也能
`python -m mini_cc.server` 当服务。但这一步埋了两个隐患:SSE 单向流,客户端断了 server
不知道;断了重连会从头派发 LLM。这两个问题直到 B8(`aaa5793`)才彻底修。HTTP 路由组装见
[07](07-http-server.md),SSE 桥与分帧见 [08](08-sse-streaming.md)。

---

## 第三步:P4 React Web UI / Step 3: React UI

提交 `475ac17 feat(web): React UI for mini_cc (P4) + resource mgmt + Playwright e2e`
(5509 行)是 mini_cc 第一次有浏览器界面。技术栈 Vite + React + react-router-dom + zustand
+ Tailwind。这步做了三件超出"做个页面"的事:

1. **HashRouter 而非 BrowserRouter。** 生产部署前端被 FastAPI 同源挂载,`#/projects/...`
   锚点路由不需要服务端配合就能深链。这决策在 P4 定下,让 Phase F 同源部署零返工。
2. **fetch + ReadableStream 手写 SSE 客户端。** `EventSource` 不支持 POST body、不能带
   `Authorization` 头。`mini_cc/web/src/lib/sse.ts` 用 `fetch()` POST + `getReader()` 手解
   SSE 帧。客户端断线重连在 B8 才补,但骨架 P4 就立住。
3. **资源管理。** Workspace 页带 FileTree、FilePreview、RunTable,第一次把"agent 在干活"
   可视化。Playwright e2e 同步引入,成为后续每轮功能的安全网。

Web UI 完整架构(路由、鉴权闸、admin、可观测性、同源部署)见 [14](14-web-ui-observability-deploy.md)。
P4 的意义不在"有了 UI",而在**第一次让 mini_cc 能被不会 curl 的人用**——这把"教学框架"
和"产品框架"的分界线明确画出来。

---

## 第四步:Phase A-F 服务端加固 / Step 4: server hardening (Phases A-F)

P0-P4 让 mini_cc "能跑",但跑不进生产。Phase A-F 六轮加固是六道"生产门"——每道都是教学
版没有、生产必须的:

**Phase A**(`a5fffa2`)——pyproject + 中途取消 + 限流 + 日志。`agent_loop()` 可能跑十几
分钟,客户端断了 server 还在烧 token。Phase A 给 SessionManager 加 cancel token,AgentLoop
每轮检查(后续 `83413f9` 补 `cancelled` SSE event,见 step 8)。限流挡住失控客户端。

**Phase B**(`68b51ba`)——跨重启会话恢复。教学版会话状态全在内存,重启全丢。Phase B 把
历史持久化到 `FSStorage`,重启后 `_ensure_warm()` 从盘上重建 AgentLoop + 历史。这是"能上线"
和"能 demo"的分水岭。

**Phase C**(`c154539`)——交互式权限提示。bash 要执行 `rm -rf` 时,教学版直接跑;生产版
必须暂停、问用户、等批准。Phase C 给工具调用加 permission gate。

**Phase D**(`6569070`)——scope / expiry / rotation。教学版 API key 是万能钥匙,泄漏全暴露。
Phase D 给 key 加 scope(`sessions:write`、`projects:read` 等)、expiry、rotation with grace
period。租户隔离 + scope 是认证授权闭环,见 [09](09-auth.md)。

**Phase E**(`6dbb92e`)——metrics + tracing + token 归因。生产要追慢请求、看 token 烧多少、
按租户计费。Phase E 加 `TraceIdMiddleware`(contextvars 注 trace_id)、`JsonFormatter`、
`MetricsMiddleware`(RED 三件套)、`tracing.log_span`(选配 OTel)。见 [14](14-web-ui-observability-deploy.md)。

**Phase F**(`00ad6e8`)——admin UI + 权限提示 UI + cold/warm 会话。把前五道门"翻译成用户
能点的界面"。

六道门的规律:**每道门都是教学版认为"用户会自己处理"而生产版必须强制的**。教学代码信任
用户;生产代码不信任任何人。

---

## 第五步:Phase G-J 能力扩展 / Step 5: capability expansion

服务端加固完,开始往回补教学版有但框架版还没有的能力。

**Phase G+H**(`4ef21eb`)——WebSearch / LSP / 动态 workflow + `/agents` 增强 + loop dispatch。
WebSearch 让 agent 查网;LSP 让 agent 看懂代码符号;动态 workflow 让 agent 运行时编排步骤。
工具箱从"文件系统 + bash"扩到"能查、能搜、能编排"。见 [05](05-tools.md)、[12](12-skills-commands-lsp.md)。

**Phase I**(`85e99ef`)——workflow 持久化 + `/resume` + MCP stdio consumer。早期 workflow
跑完就丢;Phase I 持久化、支持 `/resume`、加 MCP stdio 消费者(mini_cc 自己也能作 MCP client
连外部 server)。见 [11](11-mcp-plugins.md)。

**Phase J**(`48b4e68`)——Web UI 适配新命令(Run Table + Chat 合并 + session_resumed)。能力
扩展带来的新交互模式需要 UI 跟上。

Phase G-J 的意义:**加固不是终点,加固是为了能安全地加能力**。先把地基(A-F)打好,再往上
盖楼(G-J),每层独立 ship 不塌。

---

## 第六步:P5 Docker 容器沙箱 / Step 6: P5 Docker container sandbox

到这步 mini_cc 能跑了,但沙箱只有 `SubprocessSandbox`——直接在 host 跑 LLM 生成的代码,
对不受信代码裸奔。P5(`a2b6386` 起,9 个提交)引入容器沙箱。

**ContainerConfig**(`627cb20`)——4 维镜像配置(distro/packages/pip/env),从 `sandbox.toml`
加载。**imagebuild**(`df28718`)——把 ContainerConfig 渲染成 Dockerfile + `docker build`
包装。**ContainerRuntime Protocol**(`17d50d5`)——`mini_cc/sandbox/runtime.py` 定义 Protocol,
`DockerRuntime` 第一个实现,`FakeRuntime` 测试替身。**TenantContainerManager**(`d92115c`)
——per-tenant 容器生命周期 + name sanitizer。**ContainerSandbox**(`1c8af6c`)——
`mini_cc/sandbox/container.py` 实现 `Sandbox` Protocol,把 exec/git 路由进容器。
**ProjectManager 接 sandbox_factory**(`2a03c53`)——装配时按租户配置选沙箱,默认不变。
**ServerRuntimeContext + auto-degrade**(`0893a5f`)——docker 不可用时对 container 租户软降级
到 subprocess,记进 `degrades`。**sandbox CLI**(`9c160a4`)。

**跨平台 WSL2 检测**(`44ddd0f`)——Windows 上 docker 通常跑 WSL2,`osdetect`
(`mini_cc/sandbox/osdetect.py`)探测可用性 + WSL2 backend,适配跨平台命令前缀。没这步,
Windows 用户开容器沙箱直接报错。

P5 的关键设计:**ContainerSandbox 实现的是 `Sandbox` Protocol,与 `SubprocessSandbox`
同接口**。上层 AgentLoop 和工具完全不知道代码跑在本地还是容器——这是 step 1 把 Storage
和 Sandbox 都抽象成 Protocol 的回报。三层沙箱完整架构见 [03](03-sandbox.md)。

---

## 第七步:P6 OpenSandbox 三层架构 / Step 7: P6 OpenSandbox three-tier

P5 让 docker 可用,但 docker 不是唯一选项——有些环境用 HTTP-based 沙箱服务更合适。P6
(`647168c` 起,task 1.1-1.9)引入 OpenSandbox——HTTP API 驱动的沙箱后端,最终形成三层架构:

**Task 1.1-1.7**(`647168c`..`54e003e`)——`OpenSandboxConfig` + env loader、
`OpenSandboxRuntime._request` + `is_available`、`ensure_running` 带 tid 元数据去重、`status`
带状态映射、`exec` via execd SSE stream、`stop`/`remove`/`list_managed`、`build_image`
委托 DockerRuntime。实现在 `mini_cc/sandbox/opensandbox_runtime.py`。

**Task 1.8**(`e0fc5de`)——`ServerRuntimeContext` 三层后端选择。
`runtime_context.py:57` 的 `_build_runtime()` 按 `MINI_CC_SANDBOX_BACKEND` 决策:`opensandbox`
优先,env 没配或不可用回落 docker;`docker` 直接;`auto`(默认)先试 opensandbox 再 docker,
都没有返回 `None`。返回 `None` 时,`runtime_context.py:111` 的 `_sandbox_factory` 对标记
`container` 的租户自动降级到 `SubprocessSandbox`,降级记进 `self.degrades`(`runtime_context.py:117`)。

**MountSpec 抽象**(`3e1d1d6`)——`mini_cc/sandbox/config.py` 定义 `MountSpec`(host/PVC/OSSFS),
`e551015` 让 OpenSandboxRuntime 翻译。让"挂目录进沙箱"成独立 concern,不绑死后端。

**有状态代码解释器**(`4596e7f` + `e47d6b1`)——`OpenSandboxInterpreter` 适配器 + `/repl`
通过 env 切换。让 mini_cc 跑"有状态"Python REPL——前一个命令的变量后一个能用。

P6 核心:**三层 fallback(opensandbox → docker → subprocess)依赖 step 1 + step 6 打好的
Protocol 地基**。`ServerRuntimeContext`(见 [03](03-sandbox.md))只换 `ContainerRuntime`
实现,上层不动。加新后端(未来 K8s/gVisor)只再实现 `ContainerRuntime`。这就是
Protocol-based 扩展点的长期价值——每加一层是叠加,不是返工。

---

## 第八步:Round-2 审计与 P0 整改 / Step 8: audit & remediation

到这步 mini_cc 功能基本齐全,但功能齐全不等于生产就绪。提交 `5282ff5 docs: land round-2
production audit report` 落了 167 行审计报告
(`docs/plans/2026-06-28-production-audit-round2.md`),系统查了 mini_cc "真上线"场景下的
漏洞。审计分 batch(batch3-batch7),每批修一类:

- **batch3**(`d8245ad`)——原子存储写 + 损坏检测 + MCP close + 后台子进程 kill。`FSStorage`
  原子写(`fs.py:146`)审计前不完全原子,断电留半截文件。
- **batch4**(`d1ad886`)——SSE queue bound + heartbeat + `Last-Event-Id` replay。无界队列
  OOM,缺 heartbeat 客户端不知道连接还活着。
- **batch5**(`e3d2295`)——loop run guard + atomic task claim + teammate graceful shutdown。
- **batch6**(`a619f35`)——hook 故障隔离 + 日志脱敏(`RedactingFilter`)+ Redis 限流。
- **batch7**(`6e08762`)——plan 审批超时 + subagent MCP + workflow lock + cron catch-up。

审计直接产出的两个 P0 修复:

**P0-6**(`2595434`)——subagent 的 `session_id` 没暴露在 task tool result。`core/subagent.py`
+ `tools/subagent.py` 各加 11 行,父 agent 能拿子 agent 的 session_id 查它的会话。审计前
这信息丢了,debug 子 agent 等于盲飞。

**P0-2**(`83413f9`)——取消时没发 `cancelled` SSE event。`core/loop.py` 加 5 行,客户端能
区分"流正常结束"和"流被取消"。

核心教训:**审计抓的是 code review 抓不到的**。code review 看"代码对不对",审计看"系统在
边界条件下发生什么"——断电、OOM、并发、超时、注入。round-2 后 mini_cc 才从"功能齐全"
变"边界条件齐全"。

---

## 第九步:Workflow V2 W1-W6 + B8 / Step 9: Workflow V2

审计后功能开发回到正轨。这轮特别之处:**每个 W 都是在写对应章节时发现的 gap**。原 workflow
实现(`workflow_v2.py` 在 `f67c73c` 时 385 行,后长到 732 行)把定义和运行混在一起,章节
作者写一半发现"这功能根本没实现",边写边补:

- **W1**(`f67c73c` + `11d798a`)——definition/run 分离 + storage 层 + HTTP routes。原来定义
  和运行是同一对象;W1 分开,让一个定义能跑多次。见 [10](10-workflow-v2.md)。
- **W2**(`8c4f897`)——checkpoint gate resolution API。checkpoint 步骤暂停等人审,W2 加
  Approve/Reject API。
- **W3**(`2c5d2ad`)——inbound webhook resolver。`webhook_wait` 步骤等外部 webhook,W3 加
  接收端解析推进。
- **W4**(`296ef40`)——email subsystem + `email_wait` resolver。等邮件到达再推进,W4 加邮件
  子系统(IMAP poll + 匹配)。
- **W5**(`f7d2df9`)——validate step。确定性状态检查——上一步输出满足条件才推进。
- **W6**(`3901a31` + `15685e6`)——可视化编辑器 + drive endpoint + e2e smoke + 从编辑器
  删定义。三栏编辑器(definitions/runs | 执行时间线 | step 检查器)。

**B8**(`aaa5793`)——SSE resume-only path + 客户端断线自动重连。这是 step 2(P3)埋的隐患
最终修复:服务端加 resume-only 路径——收到 `body.resume=true` 跳过 lock + LLM 派发,只从
per-session 事件日志回放未送达事件;客户端 `sse.ts` 加指数退避重连(1s→2s→4s),重试带
`Last-Event-Id` + `resume: true`。审计前 reconnect 会重复烧 token。见 [08](08-sse-streaming.md)。

W1-W6 规律:**写文档是最好的 gap 发现器**。每写一章就要把对应子系统从里到外捋一遍,捋的
过程暴露的"该有但没有"就是 W1-W6。这也是为什么 step 10 把"写完 14 章教程"当最后一道生产门。

---

## 第十步:14 章进阶教程 / Step 10: 14-chapter tutorial

提交 `d464d00 docs(mini_cc): 14-chapter bilingual advanced internals tutorial`(本系列)
是 mini_cc 演进最后一道门。14 章中英双语,每章按五段式(问题与动机 → 设计与实现 → 操作与
验证 → 常见陷阱 → 小结)展开,带 file:line 引用、可运行 curl/playwright 步骤、真实踩过的坑。

为什么"写教程"是生产门?因为**代码能跑不等于代码能被理解,而生产级框架必须能被理解**。
逐模块拆解的教程逼着作者回答:这 Protocol 为什么这么定义?这边界为什么这么切?这不变量
为什么不能破?答不出,说明设计是偶然的,不是必然的——偶然设计在下个 PR 就塌。W1-W6 就是
教程写作暴露的 gap,B8 是写 [08](08-sse-streaming.md) 时暴露的 reconnect 重复派发——教程
是最后一道审计,只不过审计对象从"边界条件"换成"设计合理性"。

教程的副产品是**完整运维手册**:每章"操作与验证"给可运行诊断步骤,"常见陷阱"给真实踩过的
坑。新 contributor 不用 reverse-engineer 源码就能上手,而 reverse-engineer 正是教学框架
和能被团队接手的生产框架之间最后一道墙。

---

## 演进中的不变量 / Invariants across the evolution

十步演进里大量代码被推倒重写,但三个不变量从头到尾没动:

1. **Protocol-based 扩展点。** `Storage`(`storage/base.py:51`)、`Sandbox`、
   `ContainerRuntime`、`Tool` 全是 Protocol + 多实现。每加一层(OpenSandbox、有状态 REPL、
   新工具)都是再实现一个 Protocol,不动上层。这让 step 6(P5)、step 7(P6)成"叠加"非
   "返工"。
2. **无模块级全局状态。** 从 `76647ac` 起,每个子系统实例 per-project,`ProjectManager._assemble()`
   (`projects/manager.py:278`)装配时 wire 全部依赖。这是多租户隔离的前提——没它,
   `SessionManager` 就得自己处理"这 lock 是哪个项目的",复杂度爆炸。
3. **租户隔离从 day 1。** `76647ac` 第一天就立 tenant-scoped 目录布局(`projects/layout.py`)。
   如果先做单租户再"加"多租户,每个调用点都要回头改。这让 Phase D scope、round-2 R2 跨租户
   测试都是"在地基上盖楼"。

**为什么这三个不变量让每步都成为可能?** 因为它们把"扩展"和"修改"分开。加 OpenSandbox 是
扩展(实现 ContainerRuntime);改 SSE resume 是修改(动 sse.py + sessions.py)。有这三个
不变量,扩展不破坏修改,修改不牵连扩展。没它们,十步演进里至少六步会塌。

---

## 演进教训 / Lessons learned

1. **审计要走在加功能前面。** `5282ff5` round-2 审计在 Phase G-J 之后才做,导致 batch3-batch7
   修一堆"加功能时埋的雷"。如果审计在 Phase F 后立刻做,W1-W6 加功能时就会自带原子写、SSE
   边界、限流。教训:**功能齐全不等于生产就绪,审计抓边界条件,不是功能正确性**。
2. **SSE resume 需要持久化层。** B8(`aaa5793`)修 reconnect 重复派发,关键不是加重连逻辑,
   而是先有 Phase B(`68b51ba`)的会话持久化 + per-session 事件日志。resume-only 路径就是
   "从事件日志回放"。没持久化层,reconnect 要么丢事件要么重复烧 token。教训:**实时流的
   健壮性建立在持久化层之上,不是流逻辑之上**。
3. **三层 fallback 需要稳定 Protocol。** P6(`e0fc5de`)三层选择(opensandbox → docker →
   subprocess)能做,因为 P5(`17d50d5`)先定了 `ContainerRuntime` Protocol。如果 P5 直接
   写死 DockerRuntime,P6 加 OpenSandbox 就要改 `ServerRuntimeContext` 内部。教训:**抽象层
   要先于多实现出现,否则第一个实现会绑死接口**。
4. **教学代码 → 生产代码必须 day 1 多租户。** `76647ac` 第一天就立 tenant-scoped 布局,不是
   先做单租户"以后再加"。如果先单租户,Phase D scope、round-2 R2 都要回头改每个调用点。
   教训:**多租户是地基,不是 feature;地基要在 day 1 浇,不能 day 100 补**。
5. **写文档是最好的 gap 发现器。** W1-W6 全部是写 [10](10-workflow-v2.md) 时发现的"该有但
   没有";B8 是写 [08](08-sse-streaming.md) 时发现的 reconnect 重复派发。教程写作逼着作者
   把每个子系统从里到外捋。教训:**功能开发到一定阶段必须配深度文档,文档会暴露 code review
   漏掉的 gap**。
6. **软降级优于硬失败。** `0893a5f` 的 auto-degrade 让 docker 不可用时 container 租户回落
   subprocess 而非报错;`runtime_context.py:117` 把降级记进 `degrades` 让运维可见。教学代码
   倾向"配置错就报错",生产代码倾向"配置错就用次优方案 + 记日志"。教训:**生产框架要把
   "硬失败"换成"软降级 + 可观测",否则单点不可用会 brick 整个服务**。

---

## 小结 / Summary

mini_cc 的演进是一条"教学 → 框架 → 加固 → 扩展 → 审计 → 文档"的链:`c6a27ef` 的 11 节
教学讲清概念 → `76647ac` 立骨架(Protocol + 多租户 + 无全局状态)→ P3/P4 穿网络边界 →
Phase A-F 过六道生产门 → Phase G-J 安全补能力 → P5/P6 叠出三层沙箱 → round-2 审计补边界
条件 → W1-W6 + B8 边写教程边补 gap → `d464d00` 用 14 章教程把"能跑"变"能被理解、能被接手"。
每步不孤立——Protocol 地基让三层沙箱成叠加非返工,持久化层让 SSE resume 成可能,多租户
day 1 让后续所有隔离功能都在地基上长。

这 14 章教程就是 mini_cc *今天的形态*,逐模块拆开。读完这章你应知道每章背后的"为什么需要
它"和"它取代了什么";回到 [01 总览](01-overview.md) 重新走一遍 01-14,你会看到不再只是
"框架长这样",而是"框架为什么长这样"。

---

[ < [14](14-web-ui-observability-deploy.md) ] [ [README](../README.md) ] · [English version](../en/15-evolution-migration.md)
