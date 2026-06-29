# 05 — 工具注册表与内置工具

[ < 上一章 ] [ 下一章 > ] · [English version](../en/05-tools.md)

## 问题与动机

Agent 循环里的模型如果没有**工具**就毫无用处 —— 工具是它能调用以读文件、跑命令、抓 URL、
管理 todo 等的动词。一个可后端集成的框架要求工具层同时具备三点：

1. **统一** —— 每个工具共享同一种形状，这样模型、dispatcher、API schema 渲染器都能泛化处理。
2. **按上下文绑定** —— 工具处理器必须闭包捕获每次调用的状态（哪个 sandbox、哪个 storage、
   哪个 session），不依赖全局变量，从而保证多租户部署的隔离。
3. **可扩展** —— 新工具（MCP 服务器、项目专属动词）必须能无缝插入，不必改动循环。

`mini_cc/tools/` 就是这一层。本章讲核心抽象（`Tool`、`ToolContext`、`FunctionTool`）、
注册/分发/渲染管线（`builtin_tools`、`dispatch`、`to_anthropic`），然后按用途把约 45 个内
置工具分组，让你不必逐个文件翻找。

## 设计与原理

### Tool 契约

每个工具实现 `Tool` 协议（`mini_cc/tools/base.py:62`）：

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict
    def handle(self, ctx: ToolContext, args: dict) -> str: ...
```

模型看到的三个字段（`name`、`description`、`input_schema`）加一个返回字符串的方法。
**工具永远返回字符串** —— 既不抛异常，也不返回结构化数据。错误被编码成 `"Error: ..."`，
让模型能据此反应和恢复。便利封装 `FunctionTool`（line 70）把任意 `(ctx, args) -> str`
可调用对象变成 Tool，并用 try/except 把异常转成 `Error:` 字符串。

### ToolContext —— 每次调用的状态

`ToolContext`（`base.py:22`）是一个 dataclass，循环在每次 `_execute_tool_calls` 时新建。
它携带处理器可能需要的一切，无需触碰全局：

```
ToolContext
├── project_id, session_id          # 身份
├── sandbox: Sandbox                # FS + bash 边界（第 04 章）
├── storage: Storage                # 持久化
├── todos: list[dict]               # 实时任务板
├── mark_todos_updated()            # 持久化 + 发事件的回调
├── skills_loader, memory_loader    # 懒加载目录
├── scheduler (cron), wakeups       # 调度
├── mcp_pool                        # 已连接的 MCP 服务器
├── teams (TeammateSpawner)         # 多 agent 消息总线
├── project_ref                     # 反向引用，用于派生子 agent
├── background_scheduler            # 长任务池
├── background_tools                # bg worker 重入用的 name->Tool 映射
├── cancel_event                    # 终止 bg 子进程的信号
└── on_subagent_event               # 嵌套循环事件汇（task 工具用）
```

因为 `ToolContext` 是可挂属性的 dataclass，循环在构建时给它附上
`ctx.workflow_dispatch`（`loop.py:790`），让 workflow 工具能派生聚焦子 agent 而不引入
循环依赖。

### 注册、分发、渲染

`mini_cc/tools/__init__.py` 里的三个函数构成管线：

| 函数 | 行 | 用途 |
|------|----|------|
| `builtin_tools()` | 27 | 返回全部有序内置工具列表（FS、bash、todo、skills、memory、cron、mcp、task、worktree、teams、subagent、web、bgtask、websearch、wakeup、workflow、repl、LSP） |
| `dispatch(tools)` | 47 | 构建 `{name: Tool}` 查找表，循环的 `_handlers` 用 |
| `to_anthropic(tools)` | 38 | 把工具渲染成 Anthropic API 的 `input_schema` 定义，供模型请求用 |

循环每次迭代都重建工具池（`_refresh_tools`，`loop.py:278`），除非调用方冻结了工具列表 ——
这就是新连接的 MCP 工具能在会话中途即时出现的原理。MCP 工具以 `mcp__<server>__<tool>`
命名，通过 `MCPPool.all_tools()` 合并进来。

### 工具调用如何流经循环

```
   模型发出 tool_use block
            │
            ▼
   AgentLoop._execute_tool_calls          (loop.py:793)
            │
            ├── yield {"type":"tool_use", ...}        ← 宣告
            │
            ├── [若 name 在 prompt_tools 中]          ← 交互式审批门
            │     PermissionInterceptor.create/wait  (第 06 章)
            │
            ├── hooks.trigger(PreToolUse, name, input) ← 拒绝就返回字符串
            │
            ├── [若 should_run_background]            ← 慢 bash 卸载
            │     BackgroundScheduler.start(...)
            │
            └── handlers[name].handle(ctx, input)    ← 真正的调用
                    │
                    └── 返回字符串
            │
            ├── 排空 subagent_events（task 工具）
            │
            └── yield {"type":"tool_result", ...}
```

dispatcher 永远不直接 import 工具实现 —— 它只走 `self._handlers[name]`，那只是一个字典。
"Unknown tool" 会变成普通字符串结果，让模型能用别的名字重试。

### 内置工具分类

约 45 个内置工具归为九族。文件和 bash 操作是根基，其余都叠在上面。

**1. 文件系统**（`tools/fs.py`）—— `read_file`、`write_file`、`edit_file`、`glob`、`grep`。
每个操作都走 `ctx.sandbox`；直接 `open()` 会绕过边界。`grep` 支持
`files_with_matches` / `content` / `count` 三种 `output_mode`。

**2. Shell 执行**（`tools/bash.py`）—— `bash`。每次调用可设 `timeout`（硬上限 600s）、可选
`cwd`（校验必须在项目根内）、`run_in_background=true` 会移交给项目的
`BackgroundScheduler`。输出在 50KB 处截断。bash 工具优先用 PATH 上的真 bash（Windows 上
是 Git Bash），因此 Unix 语法到处可用。

**3. 后台任务**（`tools/background.py`、`tools/bgtask.py`）—— `task_output`、`task_stop`。
调度器自动检测慢操作（`is_slow_operation` 匹配 `install`、`build`、`test`、`deploy`、
`pytest` 等）并卸载；结果作为 `<task_notification>` 注入到后续轮次。显式
`run_in_background` 标志则无视检测强制卸载。

**4. Web**（`tools/web.py`、`tools/websearch.py`）—— `web_fetch`（urllib，50KB 上限，默认
20s 超时）与 `web_search`（设置了 `TAVILY_API_KEY` 时走 Tavily，否则返回明确的 "not
configured" 错误，让 agent 回退到 `web_fetch`）。

**5. 规划与记忆**（`tools/todo.py`、`tools/memory.py`、`tools/skills.py`）——
`todo_write`（持久化到 `ctx.todos`，发 `todos_updated`）；
`memory_write`/`memory_recall`/`memory_list`；`load_skill`。循环在连续 3 轮没有
`todo_write` 后注入 `<reminder>Update your todos.</reminder>` 提醒。

**6. 子 agent 与团队**（`tools/subagent.py`、`tools/task.py`、`tools/teams.py`）—— `task`
工具通过 `spawn_subagent`（第 04 章）派生聚焦子 agent，工具集冻结为文件系统 + bash。团队
工具（`send_message`、`check_inbox`、`spawn_teammate`、`submit_plan`、`request_plan`、
`review_plan`、`list_teammates`、`request_shutdown`）走 `ctx.teams`（TeammateSpawner），后者
持有 MessageBus。任务系统工具（`create_task`、`list_tasks`、`get_task`、`claim_task`、
`complete_task`）管理带依赖门控的持久工作项。

**7. 调度**（`tools/cron.py`、`tools/wakeup.py`）—— `schedule_cron`/`list_crons`/
`cancel_cron`（5 字段 cron，跨重启持久化）对比 `schedule_wakeup`/`list_wakeups`/
`cancel_wakeup`（秒级精度，仅内存，上限 3600s —— 更长延迟请用 cron）。循环每次迭代都 tick
调度器，把触发的 prompt 作为 user 消息注入。

**8. 代码执行**（`tools/repl.py`）—— `execute_code` 支持 `python`（有状态：变量按 session
pickle 到 `.mini_cc/repl/` 下）、`javascript` 和 `shell`（无状态一次性）。统一走
`ctx.sandbox.execute()`，因此容器沙箱透明地获得该能力。

**9. Worktree 与 workflow**（`tools/worktree.py`、`tools/workflow.py`）——
`create_worktree`/`keep_worktree`/`remove_worktree` 用于隔离的 git worktree；
`workflow_create`/`workflow_add_step`/`workflow_run_step`/`workflow_run_all`/
`workflow_status`/`workflow_set_state` 用于声明的多步计划，每步通过
`ctx.workflow_dispatch` 派生子 agent。

外加 `connect_mcp`（`tools/mcp.py`）和 LSP 工具 —— 集成类动词。

### 添加自定义工具

```python
from mini_cc.tools import FunctionTool, ToolContext, builtin_tools

def _my_tool(ctx: ToolContext, args: dict) -> str:
    return f"hello {args.get('name')}"

MY_TOOL = FunctionTool(
    name="hello",
    description="打个招呼。",
    input_schema={"type": "object",
                  "properties": {"name": {"type": "string"}},
                  "required": ["name"]},
    fn=_my_tool,
)

# 构建项目时注册：
tools = builtin_tools() + [MY_TOOL]
loop = AgentLoop(project, session_id, tools=tools, ...)
```

传入冻结的 `tools` 列表会关闭每迭代刷新 —— 当你完全掌控工具集时可以接受。

## 操作与配置

### 与工具相关的环境变量

| 变量 | 影响的工具 |
|------|-----------|
| `TAVILY_API_KEY` | `web_search`（Tavily 必需） |
| `MINI_CC_MCP_SERVERS` | 项目加载时自动连接的 MCP 服务器；其工具以 `mcp__*` 出现 |
| `ANTHROPIC_API_KEY` / `MODEL_ID` | 决定 `connect_mcp` 与工具调用能否被服务 |

### 各工具的可调项（硬编码，见源码）

| 常量 | 文件 | 默认值 |
|------|------|--------|
| `MAX_TIMEOUT_SECONDS` | `tools/bash.py:21` | 600 |
| `DEFAULT_TIMEOUT_SECONDS` | `tools/bash.py:22` | 120 |
| `MAX_BYTES` | `tools/web.py:16` | 50,000 |
| `DEFAULT_TIMEOUT` (web) | `tools/web.py:17` | 20 |
| `MAX_DELAY_SECONDS` | `tools/wakeup.py:20` | 3600 |
| `SLOW_KEYWORDS` | `tools/background.py:26` | install、build、test、deploy 等 |

### 可观测性端点

工具通过 SSE 事件流浮出（第 04 章）。`tool_use` 与 `tool_result` 事件携带 `name`、
`input`、`id`、`content`。`background_notification` 事件表示后台任务完成；配合
`task_output` 取结果。

## 验证步骤

```bash
# 1. 让模型调用工具，看它能看到哪些工具
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"调用 read_file 读 package.json 并总结。"}'
# 期望：tool_use(read_file) → tool_result → 文本总结 → done

# 2. 测试带后台卸载的 bash
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"运行：npm install。必要时用 run_in_background。"}'
# 慢关键字触发 BackgroundScheduler；返回 bg_id，结果在下一轮到达

# 3. 验证自定义工具出现在渲染后的 schema 里
python -c "from mini_cc.tools import builtin_tools, to_anthropic; \
  import json; print(json.dumps([t['name'] for t in to_anthropic(builtin_tools())]))"

# 4. MCP 往返：连接后调用 mcp__ 工具
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"connect_mcp name=docs，然后列出 docs 暴露了哪些工具。"}'
```

## 常见坑与调试

1. **`Unknown tool: <name>`** —— dispatcher 字典查不到。要么工具列表在 MCP 连接前就被冻结，
   要么名字拼错。MCP 工具是 `mcp__<server>__<tool>`（双下划线），不是单下划线。
2. **工具返回 `Error: ...` 但模型无视** —— 这是设计如此；模型应当读错误字符串并重试。如果
   它死循环，错误内容就是杠杆 —— 让错误信息可操作。
3. **后台任务结果永不到达** —— `BackgroundScheduler` 是按项目的；若 `ctx.background_scheduler`
   为 None，调用会回退到同步执行且无通知。检查项目构建时是否挂了 scheduler。
4. **`run_in_background=true` 静默同步执行** —— 同一根因：没有 `BackgroundScheduler`。bash
   工具在此情况下会返回明确的错误字符串（`tools/bash.py:42`）。
5. **自定义工具异常消失** —— `FunctionTool.handle` 捕获所有异常并返回
   `"Error: <Type>: <msg>"`。要拿到栈回溯，注册一个 `PostToolUse` 审计 hook（第 06 章），
   或在处理器里抛之前挂上 logger。

## 延伸阅读

- 源码：`mini_cc/tools/base.py`、`mini_cc/tools/__init__.py`，以及 `mini_cc/tools/` 下按
  类别分的各模块。
- 姊妹章节：[04 — Agent 循环](../zh/04-agent-loop.md)、[06 — 权限](../zh/06-permissions.md)
- 关于沙箱内部（bash/文件操作实际执行的地方），参见 `mini_cc/sandbox/` 包与
  `sNN-*` 概念系列。
