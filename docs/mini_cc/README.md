# mini_cc 进阶内部原理 · Advanced Internals

> 14 章中英双语教程,逐模块拆解 mini_cc —— 一套分层清晰、可嵌入、可观测、可扩展的 Claude Code 复刻框架。
>
> A 14-chapter bilingual deep dive that dissects mini_cc module-by-module — a cleanly-layered, embeddable, observable, and extensible reimplementation of Claude Code.

本教程面向已经读过 [Claude Code 官方文档](https://docs.claude.com/en/docs/claude-code/overview)、
想要理解"一个 Claude Code-style agent 框架内部到底怎么转起来"的开发者。每一章都按统一的
五段式展开:**问题与动机 → 设计与实现(含 file:line 引用) → 操作与验证 → 常见陷阱 → 小结**。
读完整套,你应当能解释 mini_cc 每条链路的"为什么这么设计"以及"在哪能扩展",并具备把这套
框架嵌入自己产品所需的全部知识。

This tutorial targets developers who have already read the
[official Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview) and want to
understand *how a Claude Code-style agent framework actually ticks internally*. Every chapter
follows the same five-part template: **Problem & motivation → Design & implementation (with
file:line citations) → Operation & verification → Pitfalls → Summary**. After finishing the
series you should be able to explain the "why this design" and "where it can be extended" of
every mini_cc link, and have everything needed to embed the framework into your own product.

---

## 这套教程是什么 / 不是什么 · What this is · What this isn't

**是什么 / What it is**

- 一份逐模块的源码导读,把 mini_cc 从存储层到 Web UI 拆成 14 个独立章节。
- 每一章都给出可运行的验证步骤(curl、playwright、Python REPL),让你不仅能"读懂",还能"跑通"。
- 中英对照,两种语言章节结构完全一致,可作为高级技术英语阅读材料。

- A module-by-module source walkthrough that splits mini_cc into 14 stand-alone chapters,
  from the storage layer up to the web UI.
- Each chapter ships runnable verification steps (curl, Playwright, Python REPL) so you can
  *run* the system, not just *read* about it.
- Bilingual, with the two language versions kept structurally identical — usable as advanced
  technical-English reading material.

**不是什么 / What it isn't**

- 不是 Claude Code 的入门教程。如果你没写过 `tools/`、没调过 LiteLLM,先回到 [01 总览](zh/01-overview.md)。
- 不是 API reference。函数签名以源码为准,这里只解释"为什么这么写"。
- 不是 release note。版本变更请看 `git log`。

- Not a Claude Code beginner tutorial. If you've never written a `tools/` or called LiteLLM,
  start at [01 overview](en/01-overview.md).
- Not an API reference. Source is authoritative for signatures — this series explains *why*
  the code is shaped the way it is.
- Not release notes. For version changes, read `git log`.

---

## 阅读路径 · Reading paths

**线性阅读 / Linear path(推荐 / recommended)** — 按 01→14 顺序读,每章前提都由前一章铺垫。

**按主题跳读 / Theme-based jumps** — 选你最关心的子系统直奔:

| 你想搞懂 / You want to understand | 起点 / Start here |
|---|---|
| 整体架构与分层决策 / overall architecture | [01](zh/01-overview.md) |
| 多租户数据如何落盘 / how multi-tenant data is stored | [02](zh/02-storage-projects-sessions.md) |
| 沙箱如何隔离 LLM 生成的代码 / sandbox isolation | [03](zh/03-sandbox.md) |
| Agent 主循环怎么派发工具 / agent loop & tool dispatch | [04](zh/04-agent-loop.md) |
| 内置工具有哪些、怎么扩展 / built-in tools & extension | [05](zh/05-tools.md) |
| 权限模型:谁能调什么 / permissions model | [06](zh/06-permissions.md) |
| HTTP server 路由组装 / HTTP routing | [07](zh/07-http-server.md) |
| SSE 流与断线重连 / SSE streaming & resume | [08](zh/08-sse-streaming.md) |
| 认证、授权、租户隔离 / auth & tenant isolation | [09](zh/09-auth.md) |
| Workflow V2:可暂停的工作流 / pausable workflows | [10](zh/10-workflow-v2.md) |
| MCP 客户端与三层插件 / MCP & three-tier plugins | [11](zh/11-mcp-plugins.md) |
| Skills、斜杠命令、LSP / skills, slash, LSP | [12](zh/12-skills-commands-lsp.md) |
| 多 Agent 团队 + cron + wakeup / teams & scheduler | [13](zh/13-teams-scheduler.md) |
| Web UI、可观测性、部署 / UI, observability, deploy | [14](zh/14-web-ui-observability-deploy.md) |

---

## 完整目录 · Full TOC

### 中文版

1. [01 — 总览:分层架构与设计决策](zh/01-overview.md)
2. [02 — 存储、项目、会话](zh/02-storage-projects-sessions.md)
3. [03 — 三层沙箱(opensandbox → docker → subprocess)](zh/03-sandbox.md)
4. [04 — Agent Loop 与工具派发](zh/04-agent-loop.md)
5. [05 — 内置工具箱](zh/05-tools.md)
6. [06 — 权限模型](zh/06-permissions.md)
7. [07 — HTTP Server 与路由](zh/07-http-server.md)
8. [08 — SSE 流与断线恢复](zh/08-sse-streaming.md)
9. [09 — 认证、授权与租户隔离](zh/09-auth.md)
10. [10 — Workflow V2(可暂停工作流)](zh/10-workflow-v2.md)
11. [11 — MCP 客户端与三层插件](zh/11-mcp-plugins.md)
12. [12 — Skills、斜杠命令与 LSP](zh/12-skills-commands-lsp.md)
13. [13 — Agent 团队与调度器](zh/13-teams-scheduler.md)
14. [14 — Web UI、可观测性与部署](zh/14-web-ui-observability-deploy.md)
15. [15 — 从教学框架到生产级框架的演进(迁移史)](zh/15-evolution-migration.md) — *跨章节的演进叙事,串起 14 章*

### English version

1. [01 — Overview: layered architecture and design decisions](en/01-overview.md)
2. [02 — Storage, projects, sessions](en/02-storage-projects-sessions.md)
3. [03 — Three-tier sandbox (opensandbox → docker → subprocess)](en/03-sandbox.md)
4. [04 — Agent loop and tool dispatch](en/04-agent-loop.md)
5. [05 — Built-in tools toolbox](en/05-tools.md)
6. [06 — Permissions model](en/06-permissions.md)
7. [07 — HTTP server and routing](en/07-http-server.md)
8. [08 — SSE streaming and resume](en/08-sse-streaming.md)
9. [09 — Authentication, authorization, and tenant isolation](en/09-auth.md)
10. [10 — Workflow V2 (pausable workflows)](en/10-workflow-v2.md)
11. [11 — MCP client and three-tier plugins](en/11-mcp-plugins.md)
12. [12 — Skills, slash commands, and LSP](en/12-skills-commands-lsp.md)
13. [13 — Agent teams and scheduler](en/13-teams-scheduler.md)
14. [14 — Web UI, observability, and deployment](en/14-web-ui-observability-deploy.md)
15. [15 — From teaching framework to production-grade (migration history)](en/15-evolution-migration.md) — *the cross-chapter evolution narrative tying the 14 together*

---

## 每章模板 · The per-chapter template

每章按下面五段式展开,中英版本结构完全一致:

1. **问题与动机 / Problem & motivation** — 这一章解决什么具体问题?没有它会怎样?
2. **设计与实现 / Design & implementation** — 源码走读,带 `file:line` 引用。
3. **操作与验证 / Operation & verification** — 可运行的 curl / playwright / REPL 步骤。
4. **常见陷阱与最佳实践 / Pitfalls & best practices** — 容易踩的坑以及规避方法。
5. **小结 / Summary** — 这章的核心一句话,以及对后续章节的指引。

Each chapter follows the same five-part template, kept structurally identical across the two
language versions:

1. **Problem & motivation** — what concrete problem does this chapter solve, and what breaks
   without it?
2. **Design & implementation** — source walk-through with `file:line` citations.
3. **Operation & verification** — runnable curl / Playwright / REPL steps.
4. **Pitfalls & best practices** — common traps and how to avoid them.
5. **Summary** — the chapter in one sentence, plus a pointer to where the trail leads next.

---

## 快速上手 · Quick start

```bash
# 后端 / backend
python -m mini_cc.server                          # 默认监听 :8000
MINI_CC_DATA_DIR=./state python -m mini_cc.server # 自定义数据目录

# 前端 / frontend
cd mini_cc/web && npm install && npm run dev      # 默认监听 :5173

# 跑测试 / tests
python -m pytest                                  # 后端单元 + 集成
cd mini_cc/web && npm test                        # 前端单元
cd mini_cc/web && npx playwright test             # 端到端
```

详细启动参数、环境变量、数据目录布局见 [第 14 章 · 部署](zh/14-web-ui-observability-deploy.md)。
For full launch flags, environment variables, and data directory layout, see
[Chapter 14 · Deployment](en/14-web-ui-observability-deploy.md).

---

## 约定 · Conventions

- 所有源码引用形如 `mini_cc/workflow_v2.py:568` —— 仓库根相对路径 + 行号。
- 命令行示例用 `$ ` 起行,Python REPL 用 `>>> ` 起行。
- 章节间的导航条位于文件首尾:`[ < prev ] [ next > ]`。
- 中英两版内容等价,但非逐句对译——技术写作以"母语读者读起来自然"为先。

- All source citations look like `mini_cc/workflow_v2.py:568` — repo-root-relative path plus
  line number.
- Shell snippets are prefixed with `$ `, Python REPL with `>>> `.
- Inter-chapter navigation lives at the top and bottom of each file: `[ < prev ] [ next > ]`.
- The two language versions are equivalent in content but not literal translations — natural
  technical prose in each language wins over word-by-word parity.

---

## 配套资料 · Companion material

- 顶层 README:`README.md`(仓库根,项目概览与快速上手)
- 部署 runbook:`docs/zh/2026-06-28-deploy-runbook.zh.md`
- 既有设计文档:`docs/{container-sandbox,plugins-3tier,stateful-repl,workflow-visualization}.md`
- 飞书 webhook 兼容性分析:`docs/zh/2026-06-29-feishu-webhook-compat-analysis.zh.md`
- e2e 覆盖矩阵:`mini_cc/web/e2e/NOTES.md` / `mini_cc/tests/e2e/NOTES.md`

---

## 反馈 · Feedback

发现错误、想补充章节、或想把这套教程移植到自己的 agent 框架上,直接在仓库提 issue 或 PR。
每一章都对应一个真实子系统,改起来比"概念文章"友好得多。

Found a mistake, want to add a chapter, or want to port this series to your own agent
framework? Open an issue or PR. Each chapter is anchored to a real subsystem, so changes are
far friendlier than editing concept-only prose.
