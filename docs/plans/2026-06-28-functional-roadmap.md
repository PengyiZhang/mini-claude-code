# mini_cc 功能性优化路线图

> **For Claude:** 本文档是功能性审阅 + 路线图，关注用户可感知的能力空缺与产品化方向。
> 不涉及安全/可靠性/性能（见 `2026-06-28-production-hardening.md`）。

**Goal:** 把 mini_cc 从「功能完备的 Agent 框架」推进到「用户用了就回不去的 Agent 平台」。

**生成时间:** 2026-06-28
**当前状态:** dev-opensandbox 分支，783 测试通过，Phase A–J + P5 + P6 全部落地。
**审计来源:** 对 `tools/` / `commands/registry.py` / `web/src/` / `core/` 的功能面盘点。

---

## 0. 功能面盘点（已有能力）

| 维度 | 已有 |
|---|---|
| Agent 能力 | 主循环 + compaction + subagent + workflow + teams + plan 审批 |
| 内置工具 | bash / fs(read·write·edit·glob·grep) / todo / repl(python·js·shell) / task / web / websearch / cron / wakeup / worktree / teams / mcp / skills / bgtask / background / lsp(9 ops) |
| 斜杠命令 | `/clear` `/sessions` `/compact` `/skills` `/mcp` `/cost` `/permissions` `/agents` `/loop` `/config` `/output-style` `/workflow` `/bg` `/resume` `/tools` 等 20+ |
| Web 页面 | 项目列表 / 工作区（聊天+文件树+待办+运行表+权限提示）/ 管理（租户/Key/指标） |
| Web 组件 | FilePreview、FileTree、MarkdownRenderer、MermaidRenderer、MessageBubble、PermissionPrompt、RunTablePanel、SessionRow、SlashMenu、Sparkline、TodoPanel、TopBar、WorkflowViewer |
| 子系统 | 三层插件发现（system/tenant/project）、cron、background、workflow 可视化（Mermaid+JSON+YAML）、LSP、有状态 REPL |

整体已超出 s20 教学版很多，但作为**通用 Agent 平台**还缺一些「用了之后就回不去」的功能。

---

## 1. 八大功能空缺

### F1. 跨会话记忆与项目知识库（最大缺口）

**现状**：每个会话独立；`skills` 是静态 markdown；没有 `CLAUDE.md`-style 项目级指南；没有跨会话用户偏好/决策积累。

**影响**：用户每次开新 session 都要重新交代背景；agent 反复犯同样错误。

**建议**：
- **`memory` 工具 + 三层记忆**：system / tenant / project 三级 `.mini_cc/memory/*.md`（复用现有 3-tier 插件发现机制）。agent 自动写入「用户偏好」「项目惯例」「已知问题」。
- **`project_guide` 工具**：读写 `.mini_cc/PROJECT.md`（等价 CLAUDE.md），自动注入 system prompt。
- **语义检索**：可选向量索引（如 chromadb），让 agent 用 `recall(query)` 取回相关历史决策。

**改动面**：
- `tools/memory.py`（新）
- `tools/project_guide.py`（新）
- `plugins/discover.py`（扩 memory 目录）
- `core/system_prompt.py`（注入 PROJECT.md）

---

### F2. 多模态输入与文档处理

**现状**：`web/src/components/` 没有 image upload；`tools/` 没有文件解析工具；FilePreview 只能预览 workspace 内已有文件。

**影响**：用户不能传张截图问「这个 UI 怎么改」、不能丢一份 PDF 问「总结一下」——这是 Claude.ai / ChatGPT 标配。

**建议**：
- **图片附件**：聊天框支持拖拽上传，转 base64 走 vision 模型（`core/llm.py` litellm 已支持 vision-capable 模型路由）。
- **`doc_parse` 工具**：解析 PDF / Word / Excel → 文本/markdown（`pypdf`、`python-docx`、`openpyxl`）。
- **OCR 兜底**：扫描件 PDF 用 `tesseract` 或外部 API（可选）。

**改动面**：
- `server/routes/uploads.py`（新，multipart 上传 + 大小限制）
- `storage/fs.py`（附件持久化 `<workspace>/.mini_cc/uploads/`）
- `tools/doc_parse.py`（新）
- `web/src/components/MessageComposer.tsx`（扩，附件按钮）
- `web/src/lib/api.ts`（upload + 转 base64 注入 messages）

---

### F3. 对话管理：搜索、导出、分支

**现状**：`/sessions` 只能列出当前项目；没有跨项目搜索；没有 export；不能从历史某条 message 分叉重跑。

**建议**：
- **`/search <query>` 命令 + 全局搜索 UI**：跨所有 session 全文检索（sqlite FTS5 或 whoosh，避免新依赖首选 sqlite）。
- **`/export [md|json]`**：导出当前 session 为 markdown（含工具调用）或 JSON 原始事件。
- **`/fork <msg_id>`**：从历史某条消息分叉新建 session，原 session 不变。
- **Session 标签**：用户给 session 打 tag（`bug`、`refactor`、`research`），按 tag 过滤。

**改动面**：
- `storage/fs.py`（扩 sessions 元数据加 tags；新建 FTS 索引）
- `commands/registry.py`（新增 `/search` `/export` `/fork` `/tag`）
- `web/src/pages/Workspace.tsx`（顶部搜索栏 + tag chip）
- `core/loop.py`（fork 重建 AgentLoop 时截断 messages）

---

### F4. 工具调用 UX

**现状**：工具结果以纯文本展示；`edit` 改动只有「Edited xxx」一句；长 bash 输出全展开撑爆聊天；background 任务进度只在 Run Table 看，不在原对话位置。

**建议**：
- **Diff 视图**：`edit` / `write` 工具改动展示成 GitHub 风格 diff（`react-diff-viewer-continued` 或自实现最小 diff）。
- **Collapsible output**：超过 N 行的工具输出自动折叠，显示「Show more (1234 lines)」。
- **Inline background progress**：发起 background 任务的对话位置插一条「live tile」，轮询显示进度，完成后变 result；不再需要切到 Run Table。
- **Tool error recovery**：工具失败时 agent 给出修复建议（如 `bash: command not found` → 自动 `/search PATH`）。

**改动面**：
- `web/src/components/MessageBubble.tsx`（diff 渲染分支 + 行数阈值折叠）
- `web/src/components/BackgroundTile.tsx`（新，SSE 订阅 + 实时进度）
- `tools/edit.py`（返回结构化 `{old, new, path}` 便于前端 diff）
- `tools/background.py`（暴露进度百分比字段）

---

### F5. 多 Agent / 子任务的体验

**现状**：`task` 工具能起子 agent；`teams` 有多 agent + plan 审批；但用户没法在 UI 上「分别看每个 agent 的对话流」。

**建议**：
- **子 agent 视图**：父对话中点开 task 调用 → 抽屉显示子 agent 的完整对话（messages、工具、token）。
- **Agent 模板**：`.mini_cc/agents/<name>.yaml` 声明不同角色（researcher、coder、reviewer），`task(role=researcher)` 直接拉起预配置 agent。
- **并行 task**：`task` 工具支持数组入参并发起多个子 agent 并行，结果合并返回。

**改动面**：
- `tools/subagent.py`（支持 role + parallel array）
- `plugins/discover.py`（扩 agents 目录）
- `core/subagent.py`（角色化 prompt 组装）
- `web/src/components/SubagentDrawer.tsx`（新）
- `server/routes/sessions.py`（暴露子 session 历史读取）

---

### F6. 模板与场景化

**现状**：每个新项目从零开始；没有场景化预设。

**建议**：
- **项目模板**：`mini_cc/templates/code-review/`、`/data-analysis/`、`/doc-writing/` —— 一键创建带预设工具/skills/prompt 的项目。
- **Prompt 快捷指令**：`/code-review`、`/summarize`、`/test-gen` —— 复合命令（predefined prompt + tool subset）。
- **Starter skill 库**：官方维护几个高质量 skill（pytest-tdd、git-flow、sql-query），用户一键安装。

**改动面**：
- `mini_cc/templates/`（新目录，含 3-5 个内置模板）
- `commands/registry.py`（新增 `/new-from-template` `/code-review` 等复合命令）
- `server/routes/projects.py`（创建时接受 template 参数）
- `web/src/pages/Projects.tsx`（模板选择器）

---

### F7. 协作与发布

**现状**：单租户内单用户视角；没有「分享 session 给同事」、没有「session 嵌入到第三方页面」、没有 webhook 通知长任务完成。

**建议**：
- **Share link**：生成只读 session URL（带签名 token），他人无需登录即可查看。
- **Embed**：iframe 嵌入到第三方应用（`/embed/<sid>` 路由，无样式 chrome）。
- **Webhook**：项目配置 webhook，cron/background/长 task 完成时回调外部系统（Slack/钉钉/企业自建）。
- **多用户协作**：同一 session 多人编辑（OT 或最后写入胜出，先实现后者）。

**改动面**：
- `server/routes/share.py`（新，签名 token 生成 + 校验）
- `server/routes/embed.py`（新）
- `server/webhooks.py`（新，事件 → HTTP 回调）
- `auth/scope.py`（扩 `share:read` scope）
- `web/src/pages/Workspace.tsx`（Share 按钮 + embed snippet 复制）

---

### F8. 开发者体验（DX）

**现状**：定位为「backend-integrable framework」，但没有官方 Python SDK 包、没有 Playground、CLI 只有 server 启动。

**建议**：
- **Python SDK**：`pip install mini-cc-client`，提供 `client = MiniCC(api_key=...); client.send(pid, "hi")` 高层 API + 自动 retry + 流式迭代器。
- **Playground**：管理后台加一个调试页面，直接调任意工具 + 看 raw 事件流，便于开发者上手。
- **CLI（非 server）**：`mini-cc run "task..."` 直接跑一次性任务（适合 CI 脚本），无需起 server。
- **官方文档站**：`docs/` 用 mkdocs，覆盖 quickstart / concepts / API / recipes。

**改动面**：
- `sdk/mini_cc_client/`（新独立包，发布 PyPI）
- `server/cli.py`（新增 `run` 子命令，本地装配 Project + AgentLoop）
- `web/src/pages/Playground.tsx`（新）
- `docs/`（mkdocs 站点）

---

## 2. 优先级建议

| 级别 | 项 | 权衡 |
|---|---|---|
| **P0（最大用户价值）** | F1 项目记忆 + F2 图片附件 + F4 diff 视图 + collapsible output | 用户每次开 session 都受益；图片是多模态基础；diff 是代码助手标配 |
| **P1（差异化竞争力）** | F3 搜索/导出 + F5 子 agent 视图 + F6 项目模板 | 把 mini_cc 从「能用」推到「好用」 |
| **P2（平台化）** | F7 协作/发布 + F8 SDK/CLI/Playground | 走向「backend-integrable」定位的兑现 |

---

## 3. 推进顺序（每阶段 2 周）

### 阶段 A（用户高频痛点）

| 子任务 | 估时 |
|---|---|
| F1.1 `.mini_cc/PROJECT.md` 自动注入 system prompt | 1 天 |
| F1.2 `memory` 工具（write/list/recall）+ 三层目录 | 3 天 |
| F4.1 diff 视图 + collapsible output | 3 天 |
| F2.1 图片附件上传 + vision 模型路由 | 3 天 |
| 配套测试 + 文档更新 | 2 天 |

**出口标准**：用户开新 session 能看到 PROJECT.md 注入；图片可上传；edit 改动有 diff；长 bash 输出可折叠。

### 阶段 B（差异化）

| 子任务 | 估时 |
|---|---|
| F3.1 `/search` + 全局 FTS | 3 天 |
| F3.2 `/export` md/json | 1 天 |
| F5.1 子 agent 抽屉视图 | 3 天 |
| F6.1 项目模板系统 | 3 天 |
| F4.2 inline background 进度 tile | 2 天 |
| F3.3 `/fork` 分支对话 | 1 天 |
| 测试 + 文档 | 2 天 |

**出口标准**：跨项目搜索可用；session 可导出/分叉；项目可从模板创建；background 进度在原对话位置可见。

### 阶段 C（平台化）

| 子任务 | 估时 |
|---|---|
| F8.1 Python SDK（独立包） | 3 天 |
| F8.2 `mini-cc run` 一次性 CLI | 2 天 |
| F7.1 share link | 2 天 |
| F7.2 webhook | 3 天 |
| F8.3 Playground 调试页 | 3 天 |
| F7.3 embed iframe 路由 | 1 天 |
| 文档站（mkdocs） | 2 天 |

**出口标准**：SDK 可 pip install 并跑通示例；session 可分享/embed；webhook 触发外部系统；Playground 能调试任意工具。

---

## 4. 不做项（明确排除）

- **语音输入 / TTS** —— 非核心，跳过。
- **Mobile 原生 App** —— Web 响应式即可。
- **认证 SSO（OAuth/SAML）** —— 已有 API key 模型，企业需要再加。
- **付费/计量** —— 商业模式问题，留给上层应用。
- **Agent marketplace** —— 生态建设留到产品验证后。
- **重写 Web UI 框架** —— React + Zustand + Tailwind 够用，仅做组件级优化。

---

## 5. 与生产化路线图的关系

本文档（功能性）与 `2026-06-28-production-hardening.md`（生产化）**互不冲突、可并行**：

- 生产化路线图关注「让现有功能稳得住」（安全、可靠性、扩展性）。
- 本路线图关注「让现有功能更好用、补齐缺失能力」（用户价值、产品化）。
- 推荐策略：**生产化阶段 0/1（P0/P1 安全+可靠性）与功能性阶段 A 并行**——两个分支独立推进，互不阻塞；阶段 B/C 待生产化阶段 0 完成后再启动（避免在不稳定地基上叠加功能）。

---

## 6. 关联文件速查

| 关注点 | 当前文件 | 改动方向 |
|---|---|---|
| 系统提示注入 | `core/system_prompt.py` | 注入 PROJECT.md |
| LLM 调用 | `core/llm.py` | vision 路由 |
| 工具注册 | `tools/__init__.py:builtin_tools` | 加 memory/doc_parse/project_guide |
| 斜杠命令 | `commands/registry.py` | 加 `/search` `/export` `/fork` `/tag` |
| 项目装配 | `projects/manager.py:_assemble` | 接受 template 参数 |
| 消息持久化 | `storage/fs.py` | sessions 元数据加 tags；FTS 索引 |
| 聊天消息渲染 | `web/src/components/MessageBubble.tsx` | diff + collapsible |
| 文件预览 | `web/src/components/FilePreview.tsx` | 图片内联渲染 |
| 项目列表 | `web/src/pages/Projects.tsx` | 模板选择器 |
| 工作区 | `web/src/pages/Workspace.tsx` | 搜索栏 + Share 按钮 |
| 路由 | `server/routes/` | 加 uploads/share/embed |
