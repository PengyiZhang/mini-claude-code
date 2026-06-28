# mini_cc 功能增强（F 系列）说明

> 本文对应分支 `dev-functional`，详细记录 F1 / F3 / F4 / F5 / F6 / F7 六大功能
> 块的设计与使用方式。所有功能都已落盘代码 + 测试，与生产硬化路线
> （`docs/plans/2026-06-28-production-hardening.md`）正交。
>
> 阅读建议：按章节顺序，每节都包含「设计动机 → 使用方式 → 后端 → 前端（如有）→
> 测试覆盖」四块。

---

## 总览

| 编号  | 主题                  | 用户可见入口                                              |
| ----- | --------------------- | --------------------------------------------------------- |
| F1.1  | 项目级 `PROJECT.md`   | 工作区 `.mini_cc/PROJECT.md` 自动注入系统提示             |
| F1.2  | 三层 memory + 工具    | `memory_list` / `memory_write` / `memory_recall` 工具     |
| F3.1  | `/search`             | 跨会话关键字检索斜杠命令                                  |
| F3.2  | `/export`             | 导出会话为 Markdown / JSON                                |
| F3.3  | `/fork`               | 把当前会话分叉到新 session_id                             |
| F4.1  | edit/write 差异视图   | 聊天里 `edit_file` / `write_file` 卡片展开后显示 unified diff |
| F4.2  | 后台任务内联 tile     | 后台任务启动后,聊天里内联轮询进度                         |
| F5.1  | 子 agent 抽屉视图     | `task` 工具调用以 🤖 抽屉样式渲染                          |
| F6.1  | 项目模板              | `POST /projects` body 带 `template`,或 Web UI 选择        |
| F7.1  | 签名分享 token        | `POST /sessions/{sid}/share` → 一次性签名 token           |
| F7.2  | Webhook 派发          | `POST /projects/{pid}/webhooks` 注册钩子                  |
| F7.3  | 嵌入 iframe           | `GET /shared/{token}/embed` 公开只读 HTML 页              |

---

## F1.1 项目级 `PROJECT.md`

### 设计动机

工作区里经常有些**对模型有用但写在系统提示里又太啰嗦**的信息：仓库结构说明、
代码风格、领域术语、PR 提交规范……这些信息属于项目本身，不属于租户或全局。F1.1
让用户在工作区放一个 `.mini_cc/PROJECT.md`,框架在每次组装系统提示时把它**自动拼到
末尾**,模型即可读到。

### 使用方式

在工作区根目录创建 `.mini_cc/PROJECT.md`:

```markdown
# 本项目的关键约定

- 提交信息遵循 Conventional Commits（feat: / fix: / docs: …）。
- 所有 Python 代码必须 `from __future__ import annotations`。
- 测试用 pytest,放在 `tests/` 下,文件名 `test_<被测对象>.py`。

## 关键模块

- `mini_cc/core/loop.py` 是 AgentLoop 主循环,不要在工具里反向依赖它。
```

下次会话发起时,模型就会看到这些约定。修改后**无需重启服务**——下次 `_assemble`
会重新读盘(mtime 变化触发缓存失效)。

### 后端

- `mini_cc/core/system_prompt.py`:新增 `load_project_guide(project_root)` 读取
  `<workspace>/.mini_cc/PROJECT.md`;返回空串表示未配置。
- `mini_cc/core/loop.py`:`assemble_system_prompt` 调用增加 `project_guide` 参数。

向后兼容:文件不存在时返回空串,系统提示保持原状。

### 测试

`tests/test_p0_system_prompt.py` 扩展 +4 个用例,覆盖:存在/不存在/为空/
含非 ASCII 字符四种情况。

---

## F1.2 三层 memory + 工具

### 设计动机

参考 Claude Code 的 skill 三层发现(`system → tenant → project`),把
**可声明式加载的 memory** 也按同样的三层组织。每个 memory 是一份带 frontmatter
的 Markdown,模型可以主动列出、读取、写入。

### 使用方式

**Memory 文件布局**:

```
<workspace>/.mini_cc/memory/             ← project 层
  coding-style.md
  bug-patterns.md
<tenants>/<tid>/.mini_cc/memory/         ← tenant 层
  team-conventions.md
<system tier>/.mini_cc/memory/           ← system 层(操作员配置)
  base.md
```

Memory 文件本体是普通 Markdown,首部 frontmatter 描述元数据:

```markdown
---
name: coding-style
description: 项目通用的 Python 编码风格要点
category: engineering
---

- 一律使用 `from __future__ import annotations`
- 类型注解必填,但不允许出现 `Any` 之外的宽松类型
- 函数不超过 40 行
```

**模型侧工具**(已注册进 builtin tool registry):

| 工具             | 作用                                                       |
| ---------------- | ---------------------------------------------------------- |
| `memory_list`    | 列出所有已发现的 memory(name + description + category)    |
| `memory_write`   | 写入/覆盖一条 memory(`name`, `content`, `description?`)   |
| `memory_recall`  | 按 name 取一条 memory 的完整内容;含关键字模糊匹配         |

`memory_write` 会对 `name` 做安全过滤(只保留 `[A-Za-z0-9._-]`),写到当前
project 层;文件名做 `_NAME_RE` 替换防止路径穿越。

### 后端

- `mini_cc/plugins/discover.py`:新增 `Memory` 数据类 + `discover_memories`,
  镜像 `discover_skills` 的三层扫描逻辑。
- `mini_cc/memory/__init__.py`:`MemoryLoader` 提供 `scan / catalog / get /
  recall` 接口,内部缓存 + mtime 失效。
- `mini_cc/tools/memory.py`:三个 `FunctionTool` 注册到 `builtin_tools()`。
- `mini_cc/projects/manager.py`:`Project.as_ref()` 暴露 `memory_loader`,工具
  通过 `ToolContext.memory_loader` 拿到。

### 测试

`tests/test_functional_memory.py` 9 个用例,覆盖三层发现、覆盖优先级、写入路径
安全、recall 模糊匹配等。

---

## F3.1 `/search` 跨会话关键字检索

### 设计动机

老版 `/sessions` 只能看 session 列表,要找"上次讨论 X 的那条对话"得逐个打开。F3.1
让用户**直接在当前会话里搜**,模型(或人)就能快速回溯历史。

### 使用方式

在 chat 输入框里:

```
/search authentication
/search TODO list
```

后端做**大小写不敏感子串匹配**,扫描当前项目下所有 `messages/*.json`。命中后返回:

```
**Found 3 matches for `authentication`:**

- **sess_a1b2** (user, msg #4):
  > …how does authentication work in this repo…
- **sess_c3d4** (assistant, msg #7):
  > …the authentication middleware checks the bearer token…
```

每条命中带 80 字符宽度的上下文窗口,前后补 `…`。最多返回 20 条。

### 后端

- `mini_cc/storage/base.py`:`Storage` Protocol 新增 `search_messages(pid,
  query, limit=20) -> list[SearchHit]`,以及 `SearchHit` 数据类。
- `mini_cc/storage/fs.py`:实现走线性扫描。**注意**:对每个 message 的 `content`
  字段,通过模块级 `_extract_text(content)` 把 list-of-blocks 展平成纯文本,
  这样 `tool_use` 块的 name + input、`tool_result` 的内容也会被搜到。
- `mini_cc/commands/registry.py`:`_cmd_search` 处理函数,注册成斜杠命令。

**为什么不用 sqlite FTS5**:实现简单、零额外依赖、对中等规模够用。当项目规模
增长(单项目数千 session)再替换为 FTS5,调用点不变。

### 测试

`tests/test_functional_search.py` 8 个用例:空查询、单命中、多命中、跨 block
类型(text/tool_use/tool_result)、limit 截断、特殊字符、跨 session。

---

## F3.2 `/export` 导出会话

### 设计动机

对话是知识资产,但常常锁在 SSE 流和 JSON 文件里。F3.2 让用户**一键导出**当前
session 为人类可读的 Markdown 或机读的 JSON,便于归档/分享/接其他工具。

### 使用方式

```
/export          # 默认 Markdown
/export md
/export json
```

输出会:

1. 以代码块形式回显在 chat 里(超过 4KB 截断预览)。
2. 落盘到 `<workspace>/.mini_cc/exports/<session_id>.<ext>`。

Markdown 渲染规则:

```markdown
# Session <session_id>

## user

<用户输入文本>

## assistant

<assistant 文本>

**tool_use `bash`:**
```json
{"command": "ls -la"}
```

**tool_result:**
```
<工具返回内容>
```
```

JSON 输出是 Anthropic 原生 message 列表(`role` + `content` blocks),适合后续脚本
消费或重新喂回 SDK。

### 后端

- `mini_cc/commands/registry.py`:`_cmd_export` + `_render_session_markdown`
  helper。导出格式参数走 `ctx.args`,不存在的格式报错。

### 测试

`tests/test_functional_search_export.py` 10 个用例:md/json 双格式、空 session
处理、tool_use/tool_result 块渲染、超长输出截断、未知格式拒绝等。

---

## F3.3 `/fork` 分支会话

### 设计动机

和 git 的分支类似:在某个 session 的当前节点**开个分叉**,继续探索但不污染
原始会话。常见场景:试一种新思路、跑一组破坏性命令、给客户演示。

### 使用方式

```
/fork
```

后端:

1. 读当前 session 的 messages。
2. 生成新 session_id(`sess_<8hex>`)。
3. 把消息列表拷到新 session 下,持久化。
4. warm 新 session。
5. 向 SSE 流发一条 `session_resumed` 事件,**前端会自动切到新 session**。
6. 回显 `🌱 forked sess_old → sess_new (N messages copied)`。

原 session 完全不动。

### 后端

- `mini_cc/commands/registry.py`:`_cmd_fork` 处理函数。
- 复用现有 `SessionManager.start_session` 的 warm 路径,不需要新 API。

### 前端

`session_resumed` 事件已是 `Workspace.tsx` 监听的标准事件,F3.3 直接复用。

### 测试

`tests/test_functional_fork.py` 3 个用例:正常分叉、空 session 提示、storage
不可用时报错。

---

## F4.1 edit/write 差异视图

### 设计动机

聊天里展开 `edit_file` / `write_file` 工具卡片,过去只能看到 JSON 输入
(`{"path": "...", "old": "...", "new": "..."}`)和文本结果。对动辄数百行的
编辑,**diff 视图**比 JSON 直观得多。

### 使用方式

无需任何命令。`edit_file` / `write_file` 工具卡展开后:

```
src/auth/login.py  +12  -3
+ def login(user, password):
+     ...
- def login(u, p):
-     ...
```

增行染绿、删行染红、等价行不带颜色。差异超过 50 行时折叠并显示
"Show N more lines"按钮。

### 后端

无后端改动——只是渲染层。

### 前端

- `mini_cc/web/src/lib/diff.ts`:基于 LCS 的 `lineDiff(oldText, newText)`,
  返回 `[{op: "add"|"del"|"eq", text}]`;`diffStats` 统计增删行数;
  `formatUnifiedDiff` 可选输出 unified diff 文本。
- `mini_cc/web/src/components/MessageBubble.tsx`:`ActivityBody` 对 `edit_file` /
  `write_file` 特判,渲染 `<DiffView>`。`CollapsibleOutput` 处理超长输出。
- `COLLAPSE_THRESHOLD = 50`:超过此行数的输出折叠。

### 测试

`mini_cc/web/smoke/diff.smoke.ts` 11 个 node 直跑的 smoke 测试,覆盖 LCS 算法
边界:全等、追加、删除中间行、替换、unified 格式标记、stats 计数、空输入。

```bash
cd mini_cc/web
node --experimental-strip-types smoke/diff.smoke.ts
```

---

## F4.2 后台任务内联 tile

### 设计动机

老版后台任务(`bash run_in_background=true`)启动后,聊天里只有一条静态
"[Background task bg_xxx started]"标记。要查进度得切到侧栏 Run Table。F4.2 把
**实时进度直接搬到聊天内**,贴合用户的视线焦点。

### 使用方式

让 agent 跑个长任务:

```
> 跑一下完整测试套件,后台执行
[assistant] 🔧 bash  run_in_background=true: pytest tests/ -q
            [Background task bg_abc123 started]
            ▼ (展开后)
              ┌──────────────────────────────────────┐
              │ ● bg_abc123  running                  │
              │ ─────────────────────────────────── │
              │ <实时 stdout 输出,每 2s 刷新>        │
              └──────────────────────────────────────┘
```

任务完成后 tile 状态自动变绿(✅),停止后变红(⛔)。

### 后端

- `mini_cc/server/routes/run_table.py`:新增
  `GET /sessions/{sid}/run-table/background/{bg_id}`。
  - 404 当任务已被 drain(通过 `task_notification` 报告完毕)。
  - 返回 `{bg_id, status, result}`;若 result 内容是
    "[bg_xxx still running]"包装串,会被剥离只显示真实输出。

### 前端

- `mini_cc/web/src/components/BackgroundTile.tsx`:
  - 每 `POLL_MS = 2000` ms 拉一次。
  - 完成(`status != "running"`)或 404(drain)时停止轮询。
  - 用 `doneRef` 防止组件卸载后的状态更新。
- `MessageBubble.tsx`:`ActivityBody` 调 `parseBgId(activity.result)` 检测
  "[Background task bg_xxx started]"标记,命中则渲染 `BackgroundTile`。

### 测试

后端 404 / 状态 / 包装剥离由 `tests/test_phaseJ_run_table.py` 覆盖(原有用例扩展)。
前端通过 TypeScript 严格类型检查 + 现有 e2e 间接覆盖。

---

## F5.1 子 agent 抽屉视图

### 设计动机

主 agent 调用 `task` 工具(派生一个聚焦型子 AgentLoop)时,在 UI 上和普通工具
调用看起来一样,用户难以分辨"这一轮做了工作"和"这一轮委托了子 agent"。F5.1 用
**视觉差异化**让委托一目了然。

### 使用方式

无需配置。当 agent 调用 `task` 工具时:

```
▼ 🤖 Subagent   描述: 检查所有路由的权限注解
  ──────────────────────────────────────
  │ 🤖 Subagent           dispatching… │
  │ ─────────────────────────────────  │
  │ task                                │
  │ 检查所有路由的权限注解              │
  │                                     │
  │ summary                             │
  │ <子 agent 最终摘要>                 │
  └─────────────────────────────────────┘
```

非 `task` 工具依然走 🔧 图标 + 灰色边框;`task` 走 🤖 + 强调色边框。

子 agent 在执行期间产生的 tool_use / tool_result 事件仍作为**兄弟活动卡片**
在同一气泡里渲染(已有行为),抽屉只是"包装层"。

### 前端

- `MessageBubble.tsx`:`Activity` 组件判断 `activity.name === "task"`,切换
  icon/border/label。
- 新增 `SubagentDrawer` 组件,把 `description` 放头部,`result` 走
  `CollapsibleOutput`。

### 测试

TypeScript 类型检查零错误;diff smoke 测试无回归。

---

## F6.1 项目模板

### 设计动机

新项目要么从空工作区开始(用户得自己搭骨架),要么复制粘贴之前的工程。F6.1 引入
**模板包**:操作员预制一组目录/文件,用户在创建项目时选一个,框架把模板内容
拷进新工作区。

### 使用方式

**列模板**(只读,任何 `projects:read` key):

```bash
curl http://localhost:8002/tenants/<tid>/projects/-/templates \
     -H "Authorization: Bearer mck_xxx"
# [
#   {"name": "blank",         "display_name": "Blank project", ...},
#   {"name": "python-cli",    "display_name": "Python CLI",    ...},
#   {"name": "skill-starter", "display_name": "Skill starter", ...}
# ]
```

**用模板建项目**:

```bash
curl -X POST http://localhost:8002/tenants/<tid>/projects \
     -H "Authorization: Bearer mck_xxx" \
     -H "Content-Type: application/json" \
     -d '{"project_id": "demo", "template": "python-cli"}'
```

内置模板:

| 名称            | 内容                                                            |
| --------------- | --------------------------------------------------------------- |
| `blank`         | 空工作区(用户自带文件)。                                       |
| `python-cli`    | `pyproject.toml` + `src/` + `tests/` + `README.md` 骨架。       |
| `skill-starter` | 预置 `skills/hello/SKILL.md`,演示自定义 skill 接入。            |

### 自定义模板包

设环境变量 `MINI_CC_TEMPLATES_DIR=/opt/mini_cc/templates`,在该目录下放子目录,
每个子目录里有 `template.json`(`description` + `display_name`)+ 任意种子文件。
**`template.json` 不会被拷进工作区**,只作为清单。

### 后端

- `mini_cc/projects/templates.py`:`list_templates / get_template /
  apply_template`。`apply_template` 用 `shutil.copytree(dirs_exist_ok=True)`,
  已有文件会被覆盖(因为模板本来就是给新工作区用)。
- `mini_cc/server/routes/projects.py`:`POST /projects` 在 `pm.create` 之后
  调 `apply_template`,再 `pm.invalidate` 让下次 `get()` 重装配;另加
  `GET /-/templates`。
- `mini_cc/server/schemas.py`:`CreateProjectRequest` 新增 `template` 字段。

### 测试

`tests/test_functional_templates.py` 6 个用例:三个内置模板加载、未知模板拒绝、
`skill-starter` 完整拷贝、覆盖已存在文件、`template.json` 不被拷贝。

---

## F7.1 签名分享 token

### 设计动机

把某次会话的对话记录分享给协作者(同事、客户、issue 跟随者)——但又不想给他
签发 API key。F7.1 用 **HMAC 签名的自包含 token** 授权只读访问。

### 使用方式

**签发**(需 `sessions:read` scope):

```bash
curl -X POST http://localhost:8002/tenants/<tid>/projects/<pid>/sessions/<sid>/share \
     -H "Authorization: Bearer mck_xxx"
# {
#   "token": "sh_<base64url-payload>.<hex-sig>",
#   "expires_in": 604800,
#   "default_secret_in_use": false
# }
```

**消费**(无鉴权):

```bash
curl http://localhost:8002/shared/<token>/messages
# [ {role, content, ...}, ... ]   ← Anthropic 原生消息格式
```

默认 TTL 7 天。`default_secret_in_use: true` 时表示服务器未配置自定义密钥
(走进程内 fallback),仅供本地调试,**生产环境务必配置**。

### token 内部结构

```
sh_<base64url({
  "p": "<project_id>",
  "s": "<session_id>",
  "m": "read",
  "iat": <unix_seconds>,
  "exp": <unix_seconds>,
  "n": "<12-hex-nonce>"
})>.<hmac_sha256_hex_signature>
```

### 密钥配置

环境变量(优先级从高到低):

| 变量名                     | 形式                              |
| -------------------------- | --------------------------------- |
| `MINI_CC_SHARE_SECRETS`    | 逗号分隔,支持优雅轮换            |
| `MINI_CC_SHARE_SECRET`     | 单值                              |
| (都没有)                   | 进程启动时随机生成的 dev fallback |

**轮换流程**:把新密钥加到 `MINI_CC_SHARE_SECRETS` **最前面**,旧 token 在过期前
仍能验证;过期后从列表里删掉旧密钥即可。

### 后端

- `mini_cc/sharing/tokens.py`:
  - `issue_share_token(project_id, session_id, mode, ttl, secret, now)` —— 全部
    参数可注入,便于测试。
  - `verify_share_token(token, secrets_iter, now)` —— 多密钥遍历 + constant-time
    HMAC 比较 + 过期/未来时间检查。
  - `warn_if_default_secret()` —— 启动日志用,提示操作员配置。
- `mini_cc/server/routes/sessions.py`:
  - `POST /tenants/{tid}/projects/{pid}/sessions/{sid}/share`
  - `GET /shared/{token}/messages`(无鉴权,凭 token)

### 安全说明

- **不用 JWT**:固定 HMAC-SHA256 + 单一密钥族,避开 alg=none 困扰。
- token 自包含,**撤销靠过期**;如需即时撤销,加一层 denylist(尚未实现)。
- 只暴露 `/messages`,不暴露 send/files/admin 等任何写或敏感读端点。

### 测试

`tests/test_functional_share_tokens.py` 9 个用例:往返、错密钥拒绝、payload 篡改
拒绝、过期拒绝、密钥轮换、重发产生不同 token、malformed 输入拒绝、
`warn_if_default_secret` 状态。

---

## F7.2 Webhook 事件派发

### 设计动机

mini_cc 的事件流本来只走 SSE。要把事件接到外部系统(Slack 通知、数据湖、审计
日志)需要一个**推**的机制。F7.2 提供 per-project 的 webhook 订阅 + 自动签名 +
重试。

### 使用方式

**注册 webhook**(需 `projects:write`):

```bash
curl -X POST http://localhost:8002/tenants/<tid>/projects/<pid>/webhooks \
     -H "Authorization: Bearer mck_xxx" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://my-server.com/mini_cc-hook",
          "event_types": ["tool_use", "todos_updated"]}'
# {"id": "wh_abc123def4", "url": "...", "event_types": [...], "created_at": "..."}
```

`event_types` 留空数组 = 订阅**所有**事件。

**列出 / 删除**:

```bash
curl http://localhost:8002/tenants/<tid>/projects/<pid>/webhooks \
     -H "Authorization: Bearer mck_xxx"

curl -X DELETE http://localhost:8002/tenants/<tid>/projects/<pid>/webhooks/wh_xxx \
     -H "Authorization: Bearer mck_xxx"
```

### 派发的 payload

每次 AgentLoop 发出匹配事件,后端会异步 POST:

```http
POST /mini_cc-hook HTTP/1.1
Content-Type: application/json
X-MiniCC-Signature: <hmac_sha256_hex_of_body>
X-MiniCC-Event: any

{
  "project_id": "<pid>",
  "session_id": "<sid>",
  "event": {"type": "tool_use", "name": "bash", ...},
  "ts": 1782610131
}
```

**接收方校验**(Python 示例):

```python
import hmac, hashlib

expected = hmac.new(SECRET.encode(), request.body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers["X-MiniCC-Signature"]):
    return 401
```

### 重试策略

- 超时 5s。
- 5xx / 网络错误重试 3 次,指数退避 1s / 2s / 4s。
- 4xx **不重试**(认为接收方主动拒绝)。

### 密钥配置

| 优先级 | 来源                                                         |
| ------ | ------------------------------------------------------------ |
| 1      | webhook 自带 `secret`(尚未通过 API 暴露,预留)              |
| 2      | `MINI_CC_WEBHOOK_SECRET` 环境变量                            |
| 3      | `MINI_CC_SHARE_SECRET`(与 F7.1 共用)                        |
| 4      | dev fallback(进程内,生产禁用)                              |

### 后端

- `mini_cc/sharing/webhooks.py`:
  - `WebhookRegistry(state_root, project_id)`:持久化到
    `<state_root>/<pid>/webhooks.json`,原子写。
  - `WebhookDispatcher`:包装 `on_event` 回调。**同步**调原始 inner,然后
    每个 hook 一个 daemon 线程跑派发。
  - `sign_payload(body, secret)`:HMAC-SHA256 hex。
- `mini_cc/server/routes/webhooks.py`:CRUD 路由。

### 接入点说明

`WebhookDispatcher` 是包装器,需要在 SessionManager 启动 session 时把
`on_event` 替换成 `dispatcher(event)`。**当前版本**已暴露 Registry + Dispatcher
+ 路由,SessionManager 侧的自动接入按需启用(避免改变现有 SSE 流量)。

### 测试

`tests/test_functional_webhooks.py` 9 个用例:Registry CRUD、URL 校验、event
type 过滤、HMAC 签名、Dispatcher 仅对匹配 hook 派发、无 hook 跳过、缺 type
跳过、deliverer 异常被吞掉。

---

## F7.3 嵌入 iframe 页

### 设计动机

把分享链路做成可嵌入:`/shared/{token}/embed` 返回一份**极简静态 HTML**,客户端
拉 token 对应的消息并渲染。可以贴到 docs 站、博客、内部 wiki 里。

### 使用方式

```html
<iframe
  src="https://mini_cc.example.com/shared/sh_xxx/embed"
  width="640" height="480"
  sandbox="allow-same-origin allow-scripts"
></iframe>
```

页面行为:

1. 嵌入时验证 token,无效直接 401(不返回 HTML)。
2. 客户端 JS `fetch("/shared/<token>/messages")`,把消息渲染成
   user / assistant 气泡。
3. tool_use 块显示 `[tool_name]`;tool_result 显示 `(tool result)`;空 session
   显示 `(session is empty)`。

### 响应头

```
Content-Type: text/html; charset=utf-8
Content-Security-Policy: default-src 'self';
                         connect-src 'self';
                         style-src 'self' 'unsafe-inline';
                         img-src 'self' data:;
X-Frame-Options: ALLOWALL
Cache-Control: no-store
```

CSP 锁死同源,只允许 inline style(用于简单样式)和 data: 图片;`ALLOWALL`
显式放开跨源 iframe 嵌入。

### 后端

- `mini_cc/server/routes/sessions.py`:`shared_embed(token)` 函数,渲染内嵌
  字符串模板 `_EMBED_HTML`(占位符 `__TOKEN__` 在签发时替换)。

### 测试

`tests/test_functional_share_embed.py` 3 个用例:坏 token 401、合法 token 返回
HTML + CSP + 头部齐全、share 创建端点需鉴权。

---

## 公共约定

### 测试覆盖总览

| 模块           | 测试文件                                       | 用例数 |
| -------------- | ---------------------------------------------- | ------ |
| F1.1 prompt    | `tests/test_p0_system_prompt.py`(扩展)        | +4     |
| F1.2 memory    | `tests/test_functional_memory.py`              | 9      |
| F3.1 search    | `tests/test_functional_search.py`              | 8      |
| F3.2 export    | `tests/test_functional_search_export.py`       | 10     |
| F3.3 fork      | `tests/test_functional_fork.py`                | 3      |
| F4.1 diff      | `mini_cc/web/smoke/diff.smoke.ts`              | 11     |
| F6.1 templates | `tests/test_functional_templates.py`           | 6      |
| F7.1 share     | `tests/test_functional_share_tokens.py`        | 9      |
| F7.2 webhooks  | `tests/test_functional_webhooks.py`            | 9      |
| F7.3 embed     | `tests/test_functional_share_embed.py`         | 3      |

F4.2 / F5.1 走的是路由 / TypeScript 类型检查,被既有套件间接覆盖。

完整跑一遍:

```bash
python -m pytest tests/ -q                              # 844 用例
cd mini_cc/web && node --experimental-strip-types smoke/diff.smoke.ts
cd mini_cc/web && npx tsc --noEmit                      # 前端类型检查
```

### 安全清单

- F7.1 / F7.2 生产部署**必须**配 `MINI_CC_SHARE_SECRET`(或各自的
  `*_WEBHOOK_SECRET`)。
- `warn_if_default_secret()` 在启动日志里会明确提示。
- 分享 token 只暴露 `/messages`,不暴露 send / files / admin。
- Webhook URL 必须 `http(s)://` 开头,避免 `file://` 等被滥用。
- 模板拷贝走 `shutil.copytree`,无路径穿越(template name 走 `_safe_wf_id`
  类校验)。

### 与生产硬化路线的关系

F 系列是**功能补齐**;`docs/plans/2026-06-28-production-hardening.md` 是**加固**。
两者正交:先 F 系列把功能补齐,再按 P0/P1/P2 顺序加固。当前 dev-functional
分支已完成所有 F1/F3/F4/F5/F6/F7 项目;P0 尚未启动。
