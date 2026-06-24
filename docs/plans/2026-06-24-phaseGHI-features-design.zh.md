# Phase G/H/I:扩展工具、动态 workflow、持久化与 MCP consumer

> 本篇覆盖 `c336fa2 → 4ef21eb → 85e99ef` 三次提交引入的能力。每节给出
> 设计动机 + 接口形态 + 使用示例 + 测试覆盖,便于在 UI/CLI/外部脚本里
> 直接对齐。

---

## Phase G:WebSearch / LSP / Wakeup / 动态 workflow / /loop 系列

### 1. WebSearch(Tavily)

**动机**:agent 在需要"最新信息"时,之前只能靠 `web_fetch` 直取页面,既
慢又容易拿到反爬页面。Tavily 是给 LLM 用的检索 API,适合搜索意图。

**配置**(`mini_cc/config.py` 读环境变量):

```bash
export TAVILY_API_KEY="tvly-..."
# 或者
export MINI_CC_TAVILY_API_KEY="tvly-..."
```

**工具签名**:

```
web_search(query: str, max_results: int = 5, search_depth: "basic"|"advanced" = "basic")
  -> 文本结果(markdown 列表)或 "Error: Tavily not configured ..."
```

未配置 key 时,工具返回明确的 "not configured" 错误文本,而不是抛异常,
agent 可以读这段文字后改用 `web_fetch` 兜底。

### 2. LSP 全量操作

**动机**:之前只能"读文件 + grep",跳定义/找引用都得靠人脑。LSP 接入
让 agent 在重构时能像 IDE 一样查符号。

**单工具多操作**:`lsp(operation, filePath, line, character, ...)`。这样
MCP/前端工具栏只需要展示一项,避免把 9 个工具都塞到模型上下文里。

| operation | 必需参数 | 返回 |
|---|---|---|
| `goToDefinition` | filePath, line, character | Location[] |
| `goToImplementation` | 同上 | Location[] |
| `findReferences` | 同上 | Location[] |
| `hover` | 同上 | Markdown 文档字符串 |
| `documentSymbol` | filePath | Symbol 树 |
| `workspaceSymbol` | query | Symbol[] |
| `prepareCallHierarchy` | filePath, line, character | CallHierarchyItem[] |
| `incomingCalls` / `outgoingCalls` | 同上 | CallHierarchyItem[] |

**语言服务器**:默认注册见 `mini_cc/lsp/__init__.py::DEFAULT_LANGUAGE_SERVERS`,
按文件扩展名挑 server(pyright/pylsp、clangd、rust-analyzer、tsserver…),
通过环境变量 `MINI_CC_LSP_<LANG>` 可覆盖。

### 3. Wakeup 调度(秒级,内存)

**动机**:cron 是分钟粒度,但 agent 经常需要"30 秒后再看一下构建",用
cron 太重,而且 cron 会落盘。`WakeupScheduler` 是 session 级的内存调度,
进程重启即消失。

**工具**:
- `schedule_wakeup(delaySeconds, prompt, reason?)` — 1–3600s 内的一次性
  唤醒;触发时把 prompt 注入到当前 session 的下一个 loop 迭代。
- `list_wakeups()` / `cancel_wakeup(wakeup_id)`。

**与 cron 的边界**:
- < 1h 且只对本 session 有意义 → `schedule_wakeup`
- 跨进程/跨重启 → `schedule_cron`(参见 `/loop`)

### 4. 动态 workflow

**动机**:有些任务模式是事前不知道完整步骤的——agent 跑完一步才知道下
一步该问什么。静态 YAML 满足不了,需要一个能在 runtime 增补 step 的
workflow。

**Workflow 模型**(`mini_cc/workflow/__init__.py`):

```python
@dataclass
class WorkflowStep:
    id: str
    prompt: str
    condition: str | None = None        # 受限 Python 表达式,基于 results 求值
    parallel_with: str | None = None
    on_failure: str = "skip"            # skip | abort | retry
    max_retries: int = 0
```

**两种定义方式**:

1. **静态 dict**(JSON 结构):
   ```json
   {"name": "research-and-draft",
    "steps": [
      {"id": "research", "prompt": "Find sources for {topic}."},
      {"id": "draft", "prompt": "Draft based on {research}.",
       "condition": "research"}
    ]}
   ```

2. **Markdown**(front-matter + `## <step-id>` 段):
   ```markdown
   ---
   name: research-and-draft
   ---
   ## research
   Find sources for {topic}.

   ## draft
   Draft based on {research}.
   ```

**工具集**(详见 `mini_cc/tools/workflow.py`):

| 工具 | 用途 |
|---|---|
| `workflow_create(workflow?, markdown?, name?)` | 创建并设为 active |
| `workflow_add_step(id, prompt, condition?, ...)` | 给 active workflow 追加 step |
| `workflow_run_step(id)` | 跑单个 step,记录 result |
| `workflow_run_all()` | 跑完所有未完成 step;首遇 `on_failure=abort` 即停 |
| `workflow_status()` | 查询状态 |
| `workflow_set_state(state)` | 合并 state 字典(用于 `{placeholder}` 替换) |

**条件求值**:走 `mini_cc.workflow._eval_condition`——禁止属性访问、禁用
builtins,falsy 自动 skip。典型写法:`condition: "research"`(上一步 result
非空时为真)。

**Placeholder**:prompt 里 `{step_id}` 会被同 id 的 step result 替换;未
知的 `{x}` 保留原样,不会抛 KeyError。

### 5. /loop / /config / /output-style

**动机**:之前这三件事都得让 agent 跑工具;现在做成 slash 命令,人能直接
敲。

- `/loop` — 列出 cron + wakeup 任务;`/loop cancel <id>` 删一个。
- `/config` — 显示当前 model / provider / API key 状态(key 做脱敏)。
- `/output-style [terse|default|detailed|streamlined]` — 影响 next turn 的
  system prompt 风格提示。

---

## Phase H:/agents 增强 / loop runner 内置 dispatch

### 1. /agents 子命令

**之前**:`/agents` 只能列活着的 teammate。

**新增**:
- `/agents` — 每行额外显示存活时长 + 收件箱未读数(`📨N`)。
- `/agents stop <name>` — 调 `spawner.request_shutdown(name)`,友好关闭。
- `/agents inbox <name>` — `bus.peek_inbox(name)` 拉收件箱,渲染成列表。

**调用流**:命令直接走 `TeammateSpawner`/`MessageBus` 接口,不通过 agent,
所以是即时返回的 server-side 命令。

### 2. workflow_dispatch 内置到 AgentLoop

**问题**:`workflow_run_step` / `workflow_run_all` 需要把 prompt 派发到
一个真实 agent。最初只能让 agent 自己再开一个 task,递归且容易死锁。

**方案**(`mini_cc/core/loop.py::_make_ctx`):构造 ToolContext 时挂一个
闭包:

```python
def _workflow_dispatch(prompt: str) -> str:
    from ..core.subagent import spawn_subagent
    return spawn_subagent(self.project, prompt, on_event=self._emit)
```

Workflow 工具拿到这个 callable 后,每跑一个 step 就开一个独立的子
AgentLoop,事件流通过 `_emit` 回灌到主 loop 的 SSE 通道。父 loop 不再
递归。

### 3. /workflow + /bg 斜杠命令

- `/workflow` — 显示 active workflow 的步骤/状态/已完成数。子命令见下
  节 Phase I。
- `/bg` — 列 background 任务(状态 emoji:🟢/✅/⛔);`/bg stop <bg_id>`
  取消一个。

---

## Phase I:Workflow 持久化 / /resume / MCP consumer

### 1. Workflow 持久化

**动机**:服务器重启后 `active_workflow` 内存丢失,长跑的 workflow(尤其
是研究/起草/审阅类的)就废了。

**Storage 新接口**(`mini_cc/storage/base.py`):

```python
save_workflow(project_id, wf_dict)        # 落盘 <state>/<project>/workflows/<wf_id>.json
load_workflow(project_id, wf_id) -> dict | None
list_workflows(project_id) -> list[dict]   # 按 saved_at 倒序
delete_workflow(project_id, wf_id) -> bool
```

**安全**:`wf_id` 走白名单过滤(只保留 `[A-Za-z0-9_-]`),阻止 `../../`
路径穿越。

**自动镜像**:`workflow_create` 创建后、`workflow_run_step` 完成 step 后,
都会调 `_persist_active(ctx, wf)` 把最新状态写盘。无需手动 save。

**`/workflow` 子命令扩展**:

```
/workflow save              # 把 active workflow 落盘
/workflow load <id|name>    # 按 id 或唯一 name 找回,设为 active
/workflow list              # 列出该 project 下所有 saved workflow
/workflow delete <id>       # 删除一个
/workflow clear             # 丢弃 active workflow(不删盘)
```

`load` 优先按 id 精确查,失败再按 name 在所有 saved 里找;命中后会把
`results`/`state`/`status` 全部还原。

### 2. /resume 斜杠命令

**动机**:一个 project 里多个 session 并存很常见(研究、实现、测试各开
一个),切换时不希望发 HTTP 自己拼 endpoint。

**接口**:

```
/resume                     # 列最近 10 个 session(按 last_active_at 倒序)
/resume <session_id>        # 预热目标 session,发出 session_resumed 事件
```

**事件**:`session_resumed { project_id, session_id }` —— 前端监听到
这个事件后,把当前激活的 session_id 切到事件里的新值即可,后端已经把
AgentLoop 在 `SessionManager._sessions` 里准备好了,后续 `send` 直连。

**冷启动**:目标 session 不在内存时,`sm.start_session(pid, sid)` 走
`_warm` 路径,从盘上拉回 messages/todos 并自动跑 `repair_dangling_tool_uses`
修复上次崩在 tool_use 中途的状态。

### 3. MCP consumer(stdio JSON-RPC)

**动机**:之前 `MCPPool` 只支持在进程内注册的"假"MCP server(测试用)。
真实场景需要拉起外部 server(filesystem / git / slack / 自家内部工具)。

**新增 `mini_cc/mcp/stdio.py::StdioMCPClient`**:

```python
client = StdioMCPClient("docs", ["npx", "mcp-server-docs"],
                        env={"API_KEY": "..."},
                        cwd="/workspace")
client.startup()                       # initialize + tools/list 握手
client.call_tool("search", {"q": "..."})  # tools/call,返回首个 text 块
client.close()                         # terminate 子进程
```

**协议细节**:
- 走 **MCP spec** 的 `Content-Length: <n>\r\n\r\n<json>` 框帧。
- 一个后台读线程按 JSON-RPC `id` 解复用响应到 `Event` + result slot。
- `tools/list` 结果缓存到 `self.tools`,通过 `MCPPool.all_tools()` 包成
  `FunctionTool` 暴露给 AgentLoop,命名规范 `mcp__<server>__<tool>`。
- 错误降级:`call_tool` 失败返回 `"MCP error: ..."` 字符串(而非抛异常),
  保持与 in-process MCPClient 一致的契约,这样 agent 拿到的就是一个
  普通文本 tool_result。

**MCPPool 集成**:

```python
ok, msg = pool.connect_stdio("docs", ["npx", "mcp-server-docs"],
                             env={"API_KEY": "..."}, cwd="/ws")
# ok=True  → 已连,后续 AgentLoop 下个 turn 自动看到新工具
# ok=False → msg 解释原因(命令找不到 / 握手超时等)
```

`connect_stdio` 失败**只返回 `(False, msg)`**,不会抛异常 —— 这很重要,
因为 `ProjectManager._assemble` 在装配 project 时会按配置批量拉起 server,
一个坏的不能让整个 project 装配失败。

**配置**(环境变量):

```bash
export MINI_CC_MCP_SERVERS='{
  "docs": {"command": ["npx", "mcp-server-docs"], "env": {"API_KEY": "x"}},
  "fs":   {"command": ["python", "-m", "mcp_server_fs"], "cwd": "/tmp"}
}'
```

解析规则(`mini_cc/config.py::_mcp_servers_from_env`):
- 整体非合法 JSON → `mcp_servers=None`(视作未配置)。
- 每个 entry 必须有 `command: list[str]`(非空),否则被过滤。
- `env`(可选 dict)、`cwd`(可选 str)透传给子进程。
- 解析时**不验证**可执行文件存在 —— 留到 `connect_stdio` 阶段才报错,
  避免误判 PATH。

**Project 自动接入**(`mini_cc/projects/manager.py::_assemble`):

project 装配结束时调 `_connect_configured_mcp_servers(pool)`,把
`default_config().mcp_servers` 里每一项都拉起来;失败的 server 静默跳过,
通过 `/mcp` 命令可以看到当前连上/可连的清单。

---

## 测试覆盖

| Phase | 测试文件 | 用例数 |
|---|---|---|
| G | `tests/test_phaseG_advanced_tools.py` | 见文件 |
| H | `tests/test_phaseH_agents_workflow.py` | 25 |
| I | `tests/test_phaseI_persistence_mcp.py` | 29 |

Phase I 的 MCP 测试用 `_FakeProc`(伪 subprocess,带后台写线程)脚本化
JSON-RPC 响应,**不依赖任何真实 MCP server 二进制**,CI 友好。

---

## 配置速查表

| 环境变量 | 作用 |
|---|---|
| `TAVILY_API_KEY` / `MINI_CC_TAVILY_API_KEY` | 启用 `web_search` |
| `MINI_CC_LSP_<LANG>` | 覆盖 `<LANG>` 的 LSP server 命令 |
| `MINI_CC_MCP_SERVERS` | JSON,启动时自动拉起的 MCP server 集合 |

## 斜杠命令速查表

```
/agents [stop <name> | inbox <name>]   # teammate 管理 + 收件箱
/bg [stop <bg_id>]                     # background 任务
/loop [cancel <id>]                    # cron + wakeup
/config                                # 当前配置概览
/output-style [terse|default|...]      # 风格提示
/workflow [save|load <id>|list|delete <id>|clear]
/resume [session_id]                   # 列/恢复 session
```
