# 06 — 权限与作用域

[ < 上一章 ] [ 下一章 > ] · [English version](../en/06-permissions.md)

## 问题与动机

一个可后端集成的 agent 框架会把不可信的模型输出（工具调用）跑在真实的文件系统和 shell 上，
并且通过 HTTP 服务多个租户。这引入了两类截然不同的授权面，绝不能混为一谈：

1. **工具级授权** —— *这次* `bash`/`write_file` 调用该不该执行，还是需要先经人审批？这是按
   session、按工具的，且天然是交互式的（模型正在轮次中等待）。
2. **HTTP 级授权** —— *这个* API key 该不该被允许调用 *这条* 路由？这是按租户、按路由的，且
   无状态（请求进来，做决定，请求继续或返回 401/403）。

`mini_cc` 把它们干净地分开。工具级提示活在 `mini_cc/core/permissions.py`（交互式
`PermissionInterceptor`）外加一个非交互式的 `make_permission_hook` 默认拒绝 hook（给不想要
提示的部署用）。HTTP 级 scope 活在 `mini_cc/auth/scope.py`，由 FastAPI 依赖层的
`require_scope` 强制。按项目的配置骑在 `<workspace>/.mini_cc/permissions.toml`。本章逐层讲
解并说明它们如何组合。

## 设计与原理

### 第一层：交互式工具提示（PermissionInterceptor）

当项目通过 `permissions.toml` 开启时，循环会拦截匹配所配置 `prompt_tools` 集合的工具调用，
并阻塞直到人做出决定。流程在 `AgentLoop._execute_tool_calls`
（`mini_cc/core/loop.py:839-868`）：

```
   模型调用 bash（且 bash ∈ prompt_tools）
            │
            ▼
   interceptor.create(session_id, "bash", input)
            │   ── 返回带 uuid4 hex id 的 PermissionRequest
            ▼
   loop yield {"type":"permission_request", request_id, ...}
            │   ── SSE 客户端渲染审批 UI
            ▼
   interceptor.wait(request_id, stop_event)
            │   ── 阻塞在 threading.Event 上，每 1s 轮询
            │      这样 session stop / cancel 能打断等待
            ▼
   三种结果之一：
     • decide(allow)  → 走正常工具执行
     • decide(deny)   → tool_result = deny_message
     • 超时/cancel    → tool_result = "[permission timed out]"
                       或 "[cancelled by session stop]"
```

interceptor 是**按项目**的（一个实例服务多个 session）。待审请求存在以 `request_id` 为键
的字典里；`list_pending(session_id)` 按 session 过滤，UI 只看到自己的提示。等待者退出时把请
求从注册表里弹出，因此已决定的请求不会残留（`mini_cc/core/permissions.py:114-119`）。

`wait()` 循环刻意采用基于轮询（默认 1s）而非单次阻塞的 `Event.wait()` —— 这样
`stop_event.is_set()`（用户点 stop 或 SSE worker 退出时被置位）能在轮询间隔内打断等待，
而不至于把循环晾到完整超时才结束。

### 第二层：非交互式默认拒绝 hook

对于不想要交互提示的部署（无头服务器、自动化管线），`make_permission_hook`
（`mini_cc/core/hooks.py:82-105`）返回一个 `PreToolUse` hook，直接拒绝危险的 bash 命令。它
有两档：

```python
# mini_cc/core/hooks.py:78-79
DENY_LIST = ("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")
DESTRUCTIVE = ("rm ", "> /etc/", "chmod 777")
```

`DENY_LIST` 永远拒绝；除非 `allow_destructive=True`，否则 `DESTRUCTIVE` 也拒绝。拒绝字符串
成为工具的 `tool_result` 内容，因此模型能看到*为什么*被拒并改用更安全的命令。这是沙箱策略
之上的纵深防御 —— 应用可以注册自己的 hook，用任意 Python 逻辑做更细的控制。

关键区别：`PreToolUse` hook 返回非 None 字符串就是**拒绝**调用；循环短路并把该字符串作为
结果 yield（`loop.py:873-876`）。交互式 interceptor 在 hook *之前*被检查，因此被人类批准
的调用即便 hook 会自动拒绝，也照样执行。

### Hooks 注册表本身

`Hooks`（`mini_cc/core/hooks.py:30`）是按项目的事件注册表，有四个事件：`UserPromptSubmit`、
`PreToolUse`、`PostToolUse`、`Stop`。每个回调都被 try/except 包住（B3），有 bug 的 hook 会
被记录并跳过，而不是拖垮整个轮次。`trigger()` 返回第一个非 None 结果 —— `PreToolUse` 拒绝
就是这样传播的。便利构造器：`make_log_hook`（审计每次调用）、`make_large_output_hook`
（输出超阈值时发信号）、`make_audit_hook`（预连好的注册表，把所有事件转发给单个汇）。

### 第三层：按项目的配置（permissions.toml）

`load_permissions_config(workspace)`（`mini_cc/projects/permissions_config.py:33`）读取
`<workspace>/.mini_cc/permissions.toml`：

```toml
prompt_tools = ["bash", "write_file", "edit_file"]
timeout_seconds = 300   # 可选；默认 300
```

文件缺失（交互提示关闭 —— 今天的默认行为）或格式错误时返回 None。`prompt_tools` 里的未知
工具名被静默忽略，使文件向前兼容。项目 manager 在构造时接线
（`mini_cc/projects/manager.py:292-295`）：若 `perm_cfg` 为 None，`project.permissions` 为
None 且 `prompt_tools` 为空集，循环的 interceptor 检查（`loop.py:840`）短路，提示永不触发。

### 第四层：HTTP scope 授权

API key 携带 `scopes` 列表（`mini_cc/auth/keys.py:53-61`，默认 `["*"]`）。语法是冒号连接的
`resource:verb`（`mini_cc/auth/scope.py:1-15`）：

| Scope | 匹配 |
|-------|------|
| `*` | 任何 |
| `read:*` | 任何 GET |
| `write:*` | 任何非 GET |
| `sessions:*` | `/sessions/...` 上的任何方法 |
| `sessions:read` | `/sessions/...` 上的 GET |
| `sessions:write` | `/sessions/...` 上的 POST/DELETE/PUT/PATCH |
| `sessions` | `sessions:*` 的简写 |

`read` ≡ GET，`write` ≡ 其余一切。映射固定在 HTTP 层，路由不必显式声明动词。
`scope_allows(held, required, method)`（`scope.py:56`）解析两侧，当任一持有 scope 对该请求
方法满足要求时返回 True。

强制执行在 `require_scope(required)`（`mini_cc/server/deps.py:56`），一个 FastAPI 依赖工厂。
`_resolve` 辅助（line 73）解析 bearer token、查 `KeyRecord`、强制租户匹配，然后调用
`scope_allows`。失败时抛 401（缺失/未知/过期 key）或 403（租户不匹配或 scope 不足）。scope
不足的 403 带 `WWW-Authenticate: Bearer scope="..."` 和 `insufficient_scope` detail 码，
方便客户端自查（`deps.py:84-91`）。

权限路由本身也受 scope 门控：`mini_cc/server/routes/permissions.py:41` 列待审
（`sessions:read`），line 56 决定（`sessions:write`）。

## 操作与配置

### 启用交互式工具提示

在 `<workspace>/.mini_cc/permissions.toml` 放一个文件：

```toml
# 只有这些工具触发提示；其余自动执行。
prompt_tools = ["bash", "write_file", "edit_file", "execute_code"]
timeout_seconds = 300
```

重启项目（或重建）—— 配置在构造时读一次。未知工具名被忽略；`prompt_tools` 里拼错会静默关
闭该工具的提示。

### 启用非交互式拒绝 hook

```python
from mini_cc.core.hooks import Hooks, make_permission_hook

hooks = Hooks()
hooks.register(Hooks.PreToolUse, make_permission_hook(allow_destructive=False))
# 构造项目 / AgentLoop 时传入 hooks=hooks。
```

若只想审计追踪而不阻断，把 `make_log_hook(sink)` 加到同一注册表。

### HTTP scope 管理

scope 在建 key 时分配（`mini_cc/auth/keys.py:97`，`generate(tenant_id, scopes=[...])`）。
`scopes=["read:*"]` 的 key 能 GET 任何东西，但不能 POST/DELETE。`*` 通配（默认）授予全部
权限。轮换的 key 继承原 key 的 scope（line 175）。

### 权限 HTTP 端点

基础路径：`/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions`

| 方法 | 路径 | Scope | 用途 |
|------|------|-------|------|
| GET | `` | `sessions:read` | 列出该 session 的待审请求 |
| POST | `/{req_id}/decide` | `sessions:write` | 批准/拒绝特定请求 |

两者在未配置 `permissions.toml`（interceptor 为 None）时返回空/404。decide body 为
`{"decision": "allow"|"deny", "message": "..."}`。

## 验证步骤

```bash
# 1. 没有 permissions.toml：无提示，decide 返回 404
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision":"allow"}' \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions/abc/decide"
# → 404

# 2. 创建 <workspace>/.mini_cc/permissions.toml，重建项目，然后：
#    让 agent 跑 bash；SSE 里出现 permission_request 事件。
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"运行：ls -la"}'
# 观察到 {"type":"permission_request","request_id":"...","tool_name":"bash",...}

# 3. 列待审，然后批准
curl -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions"
# → [{"request_id":"<id>",...}]

curl -X POST -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision":"allow"}' \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/permissions/<id>/decide"
# → 204；被阻塞的 bash 调用现在继续执行

# 4. Scope 强制：建一个只读 key，尝试 POST
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $READONLY_KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"hi"}'
# → 403，带 insufficient_scope detail
```

## 常见坑与调试

1. **提示永不触发** —— interceptor 为 None，因为 `permissions.toml` 缺失、格式错误或放错
   workspace。文件必须在 `<workspace>/.mini_cc/permissions.toml`，构造时读一次；改了要重启。
2. **`permission_request` 出现了，但 decide 返回 404** —— 请求已经被弹出（决定、超时或
   取消）。`wait()` 退出时移除条目，因此慢 UI 先轮询 list_pending 再 decide 可能与超时竞
   争。用实时 SSE 事件里的 `request_id`，别用过期的列表。
3. **提示永久阻塞** —— 检查 `timeout_seconds`（默认 300）。若客户端从不连 SSE 流，循环会
   等满时长才 yield `[permission timed out]` tool_result。自动化环境调低超时，或干脆别配
   `prompt_tools`。
4. **`make_permission_hook` 拒绝但模型一直重试** —— 拒绝字符串是模型唯一的信号。让它可操
   作："Permission denied: destructive command (matched 'rm '); override with
   allow_destructive=True" 告诉模型*改什么*，而不只是说失败了。
5. **`*` key 上 403 带 `insufficient_scope`** —— key 的租户与路径租户不匹配
   （`rec.tenant_id != tid` 抛同样的 403）。查 `WWW-Authenticate` 头和错误体里的 `held`
   列表，以区分 scope 不匹配还是租户不匹配。

## 延伸阅读

- 源码：`mini_cc/core/permissions.py`、`mini_cc/core/hooks.py`、
  `mini_cc/projects/permissions_config.py`、`mini_cc/auth/scope.py`、
  `mini_cc/auth/keys.py`、`mini_cc/server/deps.py`、
  `mini_cc/server/routes/permissions.py`
- 姊妹章节：[04 — Agent 循环](../zh/04-agent-loop.md)、[05 — 工具](../zh/05-tools.md)
- 关于沙箱策略层（deny-list hook 之下的第三道纵深防御），参见 `mini_cc/sandbox/` 与
  `sNN-*` 概念系列。
