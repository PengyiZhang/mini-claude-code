[ < [10](10-workflow-v2.md) ] [ [12](12-skills-commands-lsp.md) > ] · [English version](../en/11-mcp-plugins.md)

# 11 — MCP 客户端与三层插件

> MCP(Model Context Protocol)是 Anthropic 推的"工具总线"标准。mini_cc 既
> 是 **MCP 客户端**(消费外部 server 暴露的工具)也是 **插件宿主**(系统/
> 租户/项目三层 `.mini_cc/` 目录)。本章拆三件事:`MCPPool` + 三种 transport
> (stdio/http/sse)、3-tier 目录布局与 discovery 合并、以及失败可见性
> (`AttemptRecord` 让 `/mcp` 能告诉你"为什么 .mcp.json 没生效")。

---

## 问题与动机

s20 的 MCP 集成只有一条路:进程内 `MCPClient` + 启动时硬编码。生产里立刻撞上
四个问题:

1. **真实 MCP server 跑在子进程或远端**。stdio transport(本地命令)、HTTP
   Streamable(远端 service)、SSE(老规范)三种 wire format 都得会,否则你接
   不了官方 registry 上 80% 的 server。
2. **配置分散在 N 个地方**。系统管理员要给所有租户装 `pyright`;租户管理员要
   给本项目组接 GitHub MCP;项目作者只想要自己 workspace 的一个搜索工具。三
   层都得能声明,且**项目 > 租户 > 系统**的覆盖顺序必须确定。
3. **Claude Code 的 `.mcp.json` 是事实标准**。用户已经有这个文件了,mini_cc
   得直接吃,而不是逼他们重写成自家格式。同时自家老的 `mcp.toml` 不能砸。
4. **失败要可见**。一个连不上的 server 不该悄悄从 `/mcp` 列表里消失 —— 否则
   用户盯着"no MCP servers registered"完全不知道自己的 `.mcp.json` 哪错了。

本章对应的源码:`mini_cc/mcp/{client,stdio,http}.py`、
`mini_cc/plugins/{discover,paths,__init__}.py`、`mini_cc/config.py`、以及项目
装配时的合并器 `mini_cc/projects/manager.py:_connect_configured_mcp_servers`。

---

## 设计与原理

### 1. MCPPool:工厂在类上,连接状态在实例上

```
                ┌─ class-level: _factories ──────────┐
                │  {"docs": lambda: MCPClient(...),   │   ← app 启动时注册
                │   "github": lambda: ...}            │
                └─────────────────────────────────────┘
                              │ available_servers()
                              ▼
MCPPool(project_id="p_demo")
   _clients: {"docs": MCPClient}        ← per-project 连接状态
   _attempts: {"fs": AttemptRecord(ok=False, msg="...")}   ← 失败可见性
```

工厂放类上,意味着所有 project 共享同一份"可连接"列表;每个 project 的
`MCPPool` 实例只持有"已连上"的 client 引用(`mcp/client.py:73`)。三种连接
入口对应三种 transport:

| 方法                | transport | 文件                |
|---------------------|-----------|---------------------|
| `connect(name)`     | 进程内    | `mcp/client.py:117` |
| `connect_stdio()`   | 子进程    | `mcp/client.py:133` |
| `connect_http()`    | HTTP      | `mcp/client.py:165` |
| `connect_sse()`     | SSE       | `mcp/client.py:193` |
| `connect_from_spec()` | 按 `spec["type"]` 分发 | `mcp/client.py:217` |

`connect_from_spec` 是 discovery 层和 pool 之间的桥梁 —— 它不抛异常,而是返回
`(False, message)`,这样单个坏配置不会让 auto-connect 循环整个崩掉
(`mcp/client.py:222`)。

### 2. 三种 transport 的 wire format

**StdioMCPClient**(`mcp/stdio.py:38`)spawn 子进程,走 LSP 风格的
`Content-Length: <n>\r\n\r\n<json>` 帧。一个后台 reader thread 按 id 把响应
demux 到 waiter:

```python
def _write_message(self, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    self._proc.stdin.write(header + body); self._proc.stdin.flush()
```

(`mcp/stdio.py:157`)。`startup()` 跑标准的 `initialize` →
`notifications/initialized` → `tools/list` 三步握手(`mcp/stdio.py:104`),
把发现的 tool 归一化成 `{name, description, inputSchema}`。

**HttpMCPClient**(`mcp/http.py:142`)走 MCP 的 Streamable HTTP:POST JSON-RPC
到单个 endpoint,接受三种响应 shape —— `application/json`(裸 JSON-RPC)、
`text/event-stream`(SSE 帧,取第一个匹配 id 的 data)、`application/json-seq`
(每行一个 JSON)。`_parse_remote_response`(`mcp/http.py:344`)统一处理。

**SseMCPClient**(`mcp/http.py:170`)是老规范:GET `url` 打开事件流,server
推一个 `endpoint` 事件告诉你 POST 到哪;之后的 request/response 都走同一条
stream。`_resolve_endpoint`(`mcp/http.py:212`)支持相对路径(按 SSE URL 的
scheme+host 解析)。

### 3. 三层目录布局

每一层长一样(`plugins/paths.py:3`):

```
<tier_root>/.mini_cc/
    skills/<name>/SKILL.md
    memory/<name>.md
    mcp.toml              ← legacy mini_cc 格式
    .mcp.json             ← Claude Code 格式
    permissions.toml
```

| 层 | 路径 | 谁拥有 |
|----|------|--------|
| **system** | `<data_dir>/.mini_cc/` | 运维 |
| **tenant** | `<data_dir>/tenants/<tid>/.mini_cc/` | 租户管理员 |
| **project** | `<workspace>/.mini_cc/` | 项目作者 |

`project_tier_dirs(data_dir, tenant_id, workspace)`(`plugins/paths.py:71`)
返回 `[system, tenant, project]` 这个顺序 —— 后者覆盖前者。

`tier_dir` 对 `tenant_id` 做正则校验 `^[A-Za-z0-9_-]+$`
(`plugins/paths.py:35`),否则一个恶意 tid 能 `../` 逃出 `tenants/` 树。

### 4. MCP server 的两种文件格式

`discover_mcp_servers(tier_dirs)`(`plugins/discover.py:340`)走三层,每层先
读 `mcp.toml`(legacy)再用 `.mcp.json` 覆盖(`_read_tier` 在
`plugins/discover.py:332`)。`.mcp.json` 是 Claude Code 格式:

```json
{"mcpServers": {
  "github": {"type":"stdio","command":"npx",
             "args":["-y","@modelcontextprotocol/server-github"],
             "env":{"GITHUB_TOKEN":"ghp_..."}}
}}
```

`mcp.toml` 是 mini_cc 原生格式,`command` 已经是 list:

```toml
[docs]
command = ["npx", "mcp-server-docs"]
[docs.env]
KEY = "value"
```

`_normalize_spec`(`plugins/discover.py:214`)把两种 shape 归一到统一的
`{type, command, env, cwd, url, headers}`。**type 推断**是关键:Claude Code
允许省略 `type` —— 有 `url` 推 http,有 `command`/`args` 推 stdio
(`plugins/discover.py:237`)。

### 5. 合并器:env override 最后跑、最终赢

`_connect_configured_mcp_servers`(`projects/manager.py:349`)是项目装配时
的合并入口:

```python
# 1) 三层 .mini_cc/ 合并(system → tenant → project)
tier_dirs = project_tier_dirs(data_dir, tenant_id, workspace)
servers = discover_mcp_servers(tier_dirs)
# 2) env / 编程式 override 最后 → 赢
env_servers = default_config().mcp_servers or {}    # from MINI_CC_MCP_SERVERS
for k, v in env_servers.items():
    if "type" not in v: v = {**v, "type": "stdio"}
    servers[k] = v
# 3) 每个 spec 派发
for name, spec in servers.items():
    pool.connect_from_spec(name, spec)
```

`MINI_CC_MCP_SERVERS` 是 JSON(`config.py:48`),shape 跟 `mcp.toml` 一致:

```json
{"docs": {"command": ["npx","mcp-server-docs"], "env": {...}}}
```

为什么 env 最后跑?运维临时 override 一个 tier 配置时,不用改文件、重启即可
生效;它本质是 escape hatch。

### 6. AttemptRecord:失败可见性

每条连接路径(无论成败)都写一条 `AttemptRecord(name, ok, message)`
(`mcp/client.py:30`):

```python
@dataclass
class AttemptRecord:
    name: str; ok: bool; message: str
```

`/mcp` 命令读 `pool.list_attempts()`(`commands/registry.py:370`),把
discovered-on-disk 但连不上的 server 单独列一节:

```
**failed to connect (discovered in .mcp.json/mcp.toml):**
- 🔴 `github` — MCP server `github`: command not found: 'npx'
  _edit the matching entry in `.mini_cc/.mcp.json` ..._
```

没有这个,失败的 server 会从 `connected` 和 `available` 两个列表里同时消失,
用户看到"no MCP servers registered"完全懵。

### 7. tool 命名空间:`mcp__<server>__<tool>`

`MCPPool.all_tools()`(`mcp/client.py:280`)把每个 MCP tool 包成
`FunctionTool`,命名 `mcp__{safe_server}__{safe_tool}`,其中非字母数字字符
被替换成 `_`(`normalize_mcp_name`,`mcp/client.py:26`)。这样 builtin tool
和 MCP tool 在 AgentLoop 的 tool pool 里就完全混在一起了,模型看到的是统一的
工具列表。

---

## 操作与配置

### 三种 transport 的 spec 字段(归一化后)

| 字段 | stdio | http/sse |
|------|-------|----------|
| `type` | `"stdio"` | `"http"` / `"sse"` |
| `command` | `["npx","-y","pkg"]` | — |
| `args` | (Claude Code 风格,合并进 command) | — |
| `env` | `{...}` | `{...}`(可选) |
| `cwd` | `"..."` | — |
| `url` | — | `"https://..."` |
| `headers` | — | `{"Authorization":"Bearer ..."}` |

### 环境变量

| 变量 | 作用 | shape |
|------|------|-------|
| `MINI_CC_MCP_SERVERS` | 程序式注册 MCP server(JSON) | `{"name":{"command":[...],"env":{...}}}` |

源:`mini_cc/config.py:48`、`:121`。

### 内置 server 模板

`mini_cc/sandbox/templates/opensandbox.mcp.json` 是 OpenSandbox MCP server 的
现成模板,`${OPEN_SANDBOX_API_KEY}` 在 spawn 时按 env 展开。

### 调试用斜杠命令

| 命令 | 用途 | 源 |
|------|------|-----|
| `/mcp` | 列出 connected / available / failed-to-connect | `commands/registry.py:370` |
| `/tools` | 列出 builtin + MCP 全部 tool | `commands/registry.py:205` |

---

## 验证步骤

```bash
# 1) 放一个最小 .mcp.json 到 project workspace
mkdir -p /tmp/ws_demo/.mini_cc
cat > /tmp/ws_demo/.mini_cc/.mcp.json <<'JSON'
{"mcpServers": {
  "time": {"type":"stdio","command":"python","args":["-c",
    "import sys,json\nprint(json.dumps({'jsonrpc':'2.0','id':1,'result':{'tools':[{'name':'now','description':'current time','inputSchema':{'type':'object'}}]}}))"]},
  "broken": {"type":"stdio","command":"this-binary-does-not-exist","args":[]}
}}
JSON

# 2) 启动后端(假设 data_dir 也在 /tmp,workspace=/tmp/ws_demo)
# 然后 warm 一个 session,在 chat 里发:
/mcp

# 期望输出:
#   connected:
#   - 🟢 time (1 tools live)
#   failed to connect (discovered in .mcp.json/mcp.toml):
#   - 🔴 broken — MCP server `broken`: command not found: 'this-binary-does-not-exist'

# 3) 用 env override 再覆盖一个
export MINI_CC_MCP_SERVERS='{"fromenv":{"command":["python","-m","mcp_server_time"]}}'
# 重启后端,在 chat 里再 /mcp —— `fromenv` 应出现在 connected 里
```

直接 Python 里测 transport:

```python
from mini_cc.mcp.client import MCPPool
pool = MCPPool("p_demo")
ok, msg = pool.connect_stdio("time", ["python","-m","mcp_server_time"])
print(ok, msg)             # True, "Connected ... Discovered N tools: now"
print(pool.all_tools())    # [FunctionTool(name="mcp__time__now", ...)]
```

---

## 常见坑与调试

1. **`.mcp.json` 改了但 `/mcp` 没变?** discovery 只在项目**装配**时跑一次
   (`projects/manager.py:327`)。改完文件要重开 session / 重启进程。skill 那
   边有 `rescan_skills` 但 MCP 没有。
2. **stdio server 一直 `command not found`?** `command` 在归一化后是 list
   (`plugins/discover.py:252`),第一项必须是 PATH 上的可执行文件。
   `npx -y pkg` 写成 `"command":"npx","args":["-y","pkg"]` 才对 —— 直接写
   `"command":"npx -y pkg"` 会把整串当可执行名。
3. **HTTP server 报 "non-object" 或 "non-JSON"?** `_parse_remote_response`
   只接 `application/json`、`text/event-stream`、`application/json-seq` 三种
   (`mcp/http.py:344`)。server 返 HTML 错误页就炸 —— 先 `curl -i` 看
   Content-Type。
4. **SSE 客户端 `endpoint event timeout`?** `SseMCPClient` 启动时 GET url,
   等 server 推 `endpoint` 事件;某些老 server 不发,或被代理吃掉了
   (`mcp/http.py:205`)。这种 server 用 `HttpMCPClient` 通常更稳。
5. **`type` 没写导致 spec 被丢弃?** `_normalize_spec` 在没有 `command` 也
   没有 `url` 时返回 `None`(`plugins/discover.py:243`),server 会被静默
   skip。先确认 spec 至少有其一。
6. **tenant_id 含 `..` 或 `/` 被拒?** `tier_dir` 校验
   `^[A-Za-z0-9_-]+$`(`plugins/paths.py:35`),防目录穿越。这是安全门,不是
   bug。

---

## 延伸阅读

- 源码:`mini_cc/mcp/{client,stdio,http}.py`、`mini_cc/plugins/{discover,paths}.py`
- 合并器:`mini_cc/projects/manager.py:349`(`_connect_configured_mcp_servers`)
- env 配置:`mini_cc/config.py:48`(`_mcp_servers_from_env`)
- 模板:`mini_cc/sandbox/templates/opensandbox.mcp.json`
- 上一章:[10 — 工作流 V2](10-workflow-v2.md)
- 下一章:[12 — 技能、斜杠命令与 LSP](12-skills-commands-lsp.md)
