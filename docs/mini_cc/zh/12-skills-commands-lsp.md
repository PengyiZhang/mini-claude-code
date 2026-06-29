[ < [11](11-mcp-plugins.md) ] [ [13](13-teams-scheduler.md) > ] · [English version](../en/12-skills-commands-lsp.md)

# 12 — 技能、斜杠命令与 LSP

> 这是 mini_cc 给 LLM 装的"三件套辅助":**Skills**(模型按需 `load_skill` 加载
> 的 markdown 包)、**斜杠命令**(用户在 chat 输入 `/foo` 触发的小动作,前端
> 或后端处理)、**LSP 集成**(把 pyright / clangd / tsserver 这类语言服务器
> 当工具暴露给模型)。三者都建在 [11 章的三层插件布局](11-mcp-plugins.md)
> 之上。

---

## 问题与动机

光给模型一堆 fs / bash 工具不够,生产里你还要回答三个问题:

1. **模型怎么知道"现在该用哪个 playbook"?** 给它塞全部 skills 上下文太贵;
   不给又不知道有啥。需要一个轻量 catalog + 按需 `load_skill` 的机制,且 skill
   要能从 system / tenant / project 三层目录里发现,项目层覆盖租户层。
2. **用户要快捷操作怎么办?** 模型一轮对话太慢、太啰嗦。"清空当前 session
   历史"、"列出所有 MCP server"、"切到另一个 session"、"导出本次对话为 md"
   这些都是用户直接想要的动作。需要一个可扩展的斜杠命令 registry,且后端命令
   的返回 shape 要跟普通 model turn 一致(SSE event 流),前端不用为命令做
   特殊渲染。
3. **怎么让模型"懂"代码,而不只是会读字符串?** grep 能找文本,但
   "这个函数被谁调用"、"这个变量的类型是啥"这种语义问题,只有 LSP 能答。要把
   LSP 的 9 个常见操作暴露成单个 `lsp` 工具,自动按文件后缀选语言服务器,且
   不能拖 `pygls` 这种重依赖进来。

对应源码:`mini_cc/skills/loader.py`、`mini_cc/commands/registry.py`、
`mini_cc/lsp/__init__.py`。三者共享 [11 章](11-mcp-plugins.md) 讲过的
`project_tier_dirs` + `discover_skills` discovery 机制。

---

## 设计与原理

### 1. SkillLoader:三层 tier_dirs + legacy 回退

`SkillLoader.__init__`(`skills/loader.py:20`)接受三种构造方式:

- `tier_dirs=[...]`(推荐):显式给一份按优先级排好的 `.mini_cc/` 目录列表
  (system 在前,project 在后);
- `data_dir + tenant_id`(便利):内部调 `project_tier_dirs` 自动构造标准
  三层;
- 都不给:legacy 模式,只扫 `<project_root>/skills/`。

`scan()`(`skills/loader.py:52`)的合并逻辑:

```python
def scan(self):
    self._registry.clear()
    tier_dirs = self._resolve_tier_dirs()
    if tier_dirs:
        self._registry.update(discover_skills(tier_dirs))
    # Legacy fallback: <project_root>/skills/
    if self._legacy_skills_dir.exists():
        self._registry.update(
            discover_skills([self._legacy_skills_dir.parent]))
```

`discover_skills`(`plugins/discover.py:94`)走 tiers,**后者覆盖前者同名
skill**。`_scan_skill_dir`(`plugins/discover.py:53`)用 `rglob("SKILL.md")`
支持嵌套布局(`skills/<category>/<name>/SKILL.md`),这是第三方 skill 包
(如 superpowers)的常见组织方式。skill 名优先用 frontmatter 的 `name`,
回退到父目录名。

`catalog()`(`skills/loader.py:70`)生成一行一个 skill 的简短列表,塞进系统
提示;模型看到感兴趣的就用 `load_skill` 工具(由 AgentLoop 注册)拉全文。

### 2. SlashCommand:client / server 双 scope

```
用户在输入框打 /foo
      │
      ▼
CommandRegistry.resolve("foo") → SlashCommand
      │
      ├─ scope="client"  → 前端自己处理(如切 tab),backend 永远看不到执行
      │
      └─ scope="server"  → POST /commands/{sid}/invoke
                              │
                              ▼
                          handler(ctx) yields SSE events
                          ({type:"text"|"done"|"error"|...})
                              │
                              ▼
                          前端用和 model turn 完全一样的渲染管线
```

`SlashCommand` dataclass(`commands/registry.py:41`)字段:

```python
@dataclass
class SlashCommand:
    name: str
    description: str
    scope: Literal["client", "server"] = "server"
    aliases: tuple[str, ...] = ()
    handler: Optional[Callable[[CommandContext], Iterator[dict]]] = None
    visible: bool = True              # alias 设 False 隐藏
```

`CommandContext`(`commands/registry.py:25`)打包 handler 需要的一切:
`project_id / session_id / tenant_id / args / project / session_manager /
storage`。每个 server handler 是一个 generator,yield 的 dict 跟
`AgentLoop.run` 同 shape —— 这是命令和普通对话能共用前端渲染的关键。

`CommandRegistry.register`(`commands/registry.py:63`)在 alias 上玩了个
小把戏:alias 注册成隐式 pointer 指回原 SlashCommand(`visible=False`),
这样 `resolve("/?")` 能透明找到 `help`。

### 3. 内置命令一览(22 个)

`default_registry()`(`commands/registry.py:1227`)在首次调用时填充 22 个
内置命令。按主题分组:

| 主题 | 命令 | 源 |
|------|------|----|
| Session 管理 | `/sessions`, `/resume`, `/fork`, `/clear`, `/compact` | `:135`, `:1155`, `:1110`, `:106`, `:453` |
| 配置查看 | `/model`, `/config`, `/cost`, `/permissions`, `/output-style` | `:155`, `:796`, `:476`, `:519`, `:836` |
| 工具 / MCP / Skills | `/tools`, `/mcp`, `/skills`, `/tasks` | `:205`, `:370`, `:172`, `:434` |
| 后台 / 调度 | `/bg`, `/loop`, `/agents` | `:1051`, `:716`, `:560` |
| 工作流(旧版) | `/workflow` 子命令 clear/save/load/list/delete | `:873` |
| 检索 / 导出 / 日志 | `/search`, `/export`, `/logs` | `:242`, `:279`, `:681` |
| 帮助 | `/help`(alias `?`) | `:93` |

`/workflow` 操作的是**旧版** Workflow(`commands/registry.py:873` 注释明确
说明),不是 [10 章](10-workflow-v2.md) 的 V2。两套并行,V2 走 HTTP API。

`/agents`(子命令 `stop <name>` / `inbox <name>`,`commands/registry.py:560`)
和 `/bg`(`stop <bg_id>`,`:1051`)演示了"带子命令"的命令模式 —— handler
自己 split `ctx.args` 决定分支。

### 4. LSP 集成:9 个操作,一个工具

`OPERATIONS`(`lsp/__init__.py:51`)把 9 个语义操作映射到 JSON-RPC method:

| Operation | LSP method |
|-----------|------------|
| `goToDefinition` | `textDocument/definition` |
| `goToImplementation` | `textDocument/implementation` |
| `findReferences` | `textDocument/references` |
| `hover` | `textDocument/hover` |
| `documentSymbol` | `textDocument/documentSymbol` |
| `workspaceSymbol` | `workspace/symbol` |
| `prepareCallHierarchy` | `textDocument/prepareCallHierarchy` |
| `incomingCalls` | `callHierarchy/incomingCalls` |
| `outgoingCalls` | `callHierarchy/outgoingCalls` |

`LSPManager`(`lsp/__init__.py:84`)是**每项目一个**的语言服务器池,懒启动:
第一次查某语言的文件才 spawn 对应 server。`DEFAULT_LANGUAGE_SERVERS`
(`lsp/__init__.py:34`)按语言列候选 binary,`_find_binary` 用 `shutil.which`
取 PATH 上第一个匹配的。

```python
DEFAULT_LANGUAGE_SERVERS = {
    "python": ["pyright-langserver", "pylsp", "jedi-language-server"],
    "typescript": ["typescript-language-server", "vtsls"],
    "rust": ["rust-analyzer"], "go": ["gopls"], "java": ["jdtls"],
    ...
}
```

每个 server 用一个 `_ServerHandle`(`lsp/__init__.py:64`)管理,内含 lock ——
**串行化请求**,因为大多数 LSP server 本来就串行处理。`_launch_args`
(`lsp/__init__.py:194`)处理 per-binary quirk:大多数接 `--stdio`,但
`rust-analyzer`/`gopls`/`jdtls` 不带参数,`solargraph` 用 `stdio`(无 dash)。

工具入口 `_lsp(ctx, args)`(`lsp/__init__.py:341`)做四件事:

1. 校验 operation 在 9 个里;
2. 通过 sandbox 解析路径(`ctx.sandbox.resolve_path` + `validate_path`),
   支持项目相对路径;
3. 按文件后缀推断 language,失败则要求显式传;
4. 取出 project 上的 `lsp_manager`,发请求,把 JSON-RPC result 格式化成
   markdown 给 LLM 看。

`_format_result`(`lsp/__init__.py:423`)针对不同 operation 定制输出:
hover 抽 `contents.value`,documentSymbol 列符号名 + kind
(`_SYMBOL_KINDS` 把数字 kind 映射成 "Function"/"Class"/...),definition /
references 列 `uri:line:char`。

**关键设计决策:不依赖 pygls**。`_read_one_message`(`lsp/__init__.py:278`)
手写 Content-Length 帧解析,`_read_response`(`lsp/__init__.py:258`)循环
读直到拿到匹配 id 的响应,中途忽略 server 推的 `window/showMessage` 等通知。
每个请求是单次幂等 round-trip,不维护 document state(server 会回退到磁盘读)。

### 5. /help 自动发现 + alias

`_cmd_help`(`commands/registry.py:93`)运行时调
`default_registry().all_visible()`,所以**新注册的命令立刻出现在帮助里**。
alias 在 `register` 时被设 `visible=False` 指回主命令,所以 `/?` 不会在帮助
里单独占一行,但 `resolve("/?")` 仍能找到 `help`。

---

## 操作与配置

### Skills 文件布局

```
<tier>/.mini_cc/skills/
    my-skill/
        SKILL.md           ← frontmatter (name, description) + 正文
    category/nested/       ← 支持任意深度嵌套
        deep-skill/SKILL.md
```

frontmatter 可选;没有的话 name 取父目录名,description 取正文第一行
(`plugins/discover.py:87`)。

### 斜杠命令注册(扩展点)

```python
from mini_cc.commands import SlashCommand, default_registry

def _my_cmd(ctx):
    yield {"type": "text", "text": f"hi {ctx.args}"}
    yield {"type": "done"}

default_registry().register(SlashCommand(
    name="hi", description="Say hi", handler=_my_cmd,
    aliases=("hello",),
))
```

插件的 `register` 必须在 app 启动时调,因为 `default_registry()` 返回的是
进程级单例(`commands/registry.py:1224`)。

### LSP server 配置

默认按 PATH 探测。要换或加 binary:

```python
from mini_cc.lsp import LSPManager
mgr = LSPManager(
    project_root=Path("/repo"),
    language_servers={"python": ["my-pyright"], "kotlin": ["kotlin-lsp"]},
)
```

无环境变量开关;LSPManager 实例挂在 `project.lsp_manager` 上,
`_get_manager`(`lsp/__init__.py:498`)从 `ToolContext.project_ref` 取。

### 文件后缀 → 语言映射

`_EXT_TO_LANGUAGE`(`lsp/__init__.py:308`)覆盖 py/js/ts/jsx/tsx/c/cpp/rs/
go/java/rb。新后缀要么传 `language` 参数,要么扩展 `language_servers`。

---

## 验证步骤

后端跑在 `:8002`,warm 一个 session,在 chat 输入:

```
/help                        # 列出所有可见命令 + alias
/skills                      # 列出本项目可用 skills
/tools                       # 列出 builtin + MCP tool
/mcp                         # 列出 connected / available / failed
/sessions                    # 列出本项目所有 session
/resume                      # 不带参数 → 列最近 10 个 session
/resume sess_xxx             # 切到指定 session
/export md                   # 导出当前 session,落 <ws>/.mini_cc/exports/<sid>.md
/search login                # 跨 session 全文搜 "login"
/agents                      # 列 teammate,看年龄 + inbox 数
/agents stop researcher      # 给名为 researcher 的 teammate 发 shutdown
/bg                          # 列后台任务
/loop                        # 列 cron + wakeup 调度
/config                      # 看有效配置(API key 自动 redact)
```

LSP 测试(前提:`pyright-langserver` 在 PATH 上):

```
# 让模型用 lsp 工具
> 用 lsp 工具找 mini_cc/workflow_v2.py 里 WorkflowService 类的所有引用
# 模型会调:
#   lsp(operation="workspaceSymbol", query="WorkflowService")
#   lsp(operation="findReferences",
#       filePath="mini_cc/workflow_v2.py", line=216, character=7)
```

直接 Python 测 LSPManager:

```python
from pathlib import Path
from mini_cc.lsp import LSPManager, OPERATIONS
mgr = LSPManager(Path("/path/to/repo"))
syms = mgr.request("python", "textDocument/documentSymbol",
                   {"textDocument": {"uri": Path("src/app.py").resolve().as_uri()}})
print([s["name"] for s in syms])
mgr.shutdown()
```

---

## 常见坑与调试

1. **skill 加了 `/skills` 看不到?** `/skills` 会调
   `project.skills_loader.scan()`(`commands/registry.py:184`),所以运行时
   重扫;但如果 skill 文件放错位置(不在 `.mini_cc/skills/<name>/SKILL.md`
   或 legacy `<root>/skills/`),扫不到。注意父目录名才是默认 skill id。
2. **命令 handler 改了不生效?** `default_registry()` 是进程级单例
   (`commands/registry.py:1224`),`register` 在 `default_registry()` 第一次
   调用时执行。改 handler 后必须重启进程。
3. **LSP `no LSP server binary found on PATH`?** `_find_binary` 只查
   `language_servers[language]` 列出的候选(`lsp/__init__.py:170`)。
   `pip install pyright` 后还要确认 `pyright-langserver` 在 PATH(不是
   `pyright` —— 那是 npm 包的 CLI,LSP 入口是 `pyright-langserver`)。
4. **LSP 请求 `timeout`?** 默认 15s(`lsp/__init__.py:233` 的 init,
   `:382` 的 request)。首次 init 大型 repo(尤其 typescript)可能慢;调
   `LSPManager.request(..., timeout=30.0)`。`_read_response` 循环读,忽略
   server 通知,只等匹配 id 的响应(`lsp/__init__.py:258`)。
5. **call hierarchy 报参数错?** `incomingCalls` / `outgoingCalls` 需要先
   `prepareCallHierarchy` 拿到 item,再传 `item` 字段(`lsp/__init__.py:404`)。
   item 必须含 `name / kind / uri / range / selectionRange`,缺省值从
   `line/character` 拼。
6. **`/workflow` 和 V2 HTTP 是两套?** 是的。`/workflow` 操作**旧版**
   `Workflow`(`commands/registry.py:873` 明确说),走 storage 的
   `save_workflow/load_workflow`;V2 见 [10 章](10-workflow-v2.md) HTTP API。
   两套数据不互通。

---

## 延伸阅读

- 源码:`mini_cc/skills/loader.py`、`mini_cc/commands/registry.py`(1352 行)、
  `mini_cc/lsp/__init__.py`(573 行)
- 三层 discovery 基础:[11 — MCP 客户端与三层插件](11-mcp-plugins.md)
  (`plugins/discover.py` 共用)
- 工作流 V2(对比 `/workflow` 的旧版):[10 — 工作流 V2](10-workflow-v2.md)
- Skill / Memory 共享 discovery:`plugins/discover.py:53`(`_scan_skill_dir`)、
  `:112`(`_scan_memory_dir`)
- LSP OPERATIONS 表:`lsp/__init__.py:51`
