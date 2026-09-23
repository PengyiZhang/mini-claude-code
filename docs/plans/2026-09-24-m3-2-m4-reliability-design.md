# M3-2 + M4 可靠性批次设计（2026-09-24）

> **范围**（与维护路线图 `2026-09-22-maintenance-roadmap.md` 对应）：
> M3-2、M4-1、M4-2、M4-3、M4-4、M4-7，共 6 项。
> M4-5（长会话加载）、M4-6（异步 IO）、M4-8（无状态化）本轮后置。
>
> **执行方式**：main 上顺序做，每项一个 TDD commit（测试先行）；M3-2 独立
> 批次不与其它改动混提交（路线图既定要求）。

## 1. M3-2 teams 拆分

`mini_cc/teams/__init__.py` 1304 行拆为四个模块，`__init__.py` 缩到
~100 行只留 re-export，所有现有导入路径（`from ..teams import ...`、
`from ..teams import MessageBus` 等）不断裂。

| 现位置 | 去处 | 内容 |
|---|---|---|
| `MessageBus` + `_format_inbox_as_dialogue` | `teams/bus.py` | mailbox 文件锁、历史、广播（~430 行） |
| `ProtocolState` / `ProtocolTracker` | `teams/protocol.py` | 请求登记与状态（~50 行） |
| `TeammateInfo` / `TeammateSpawner` | `teams/spawner.py` | 生命周期、runner、idle_poll、任务认领（~760 行） |
| `_CONVENTION_PROMPT` / `convention_prompt()` | `teams/convention.py` | 团队协作约定提示词（~60 行） |

Spawner↔Bus/Tracker 耦合保持构造注入不变。纯移动不改行为；拆完跑
全量测试守护（teams 相关 15+ mailbox 测试与 spawner 测试全绿）。

## 2. M4-1 + M4-2 错误分类器（共享实现，一个 commit）

`core/recovery.py` 新增：

```python
@dataclass(frozen=True)
class ErrorClass:
    kind: str        # rate_limit|overloaded|network|quota|auth|invalid_request|unknown
    transient: bool  # True → 可重试；False → 立即失败

def classify_error(e: Exception) -> ErrorClass: ...
```

分类表（按异常名/消息子串匹配，与现有 429/529 识别风格一致）：

| kind | transient | 识别特征 |
|---|---|---|
| rate_limit | True | 429 / ratelimit |
| overloaded | True | 529 / overloaded |
| network | True | timeout / connection / eof |
| quota | False | quota / credit / balance / 402 |
| auth | False | invalid_api_key / authentication / 401 / permission |
| invalid_request | False | invalid_request_error / 400 / not_found_error / modelNotFound |
| unknown | False | 兜底（不重试，与现状一致） |

- **M4-2**：`with_retry` 循环开头 `cls = classify_error(e)`；
  transient 且是 rate_limit/overloaded 走现有退避重试；permanent 直接
  `raise`（带分类信息的事件先发）。
- **M4-1**：loop.py 的 except 路径调用 `classify_error`；
  `{"type":"error"}` 事件增加 `error_class`/`transient` 字段；transcript
  文本块前缀 `[Error][transient] {type}: {e}`（仅瞬态加标记），消息
  schema 不动，现有客户端无需适配。

## 3. M4-7 保守并行工具执行

- `tools/base.py` 的 Tool 基类加 `parallel_safe: bool = False`——
  显式声明制，默认不并行。
- 初期白名单（全部只读）：fs 读取类（read/grep/glob/ls 类）、
  web fetch/search 类。写操作、bash、todo_write、task（子代理）、
  后台卸载路径一律不并行。
- `loop.py::_execute_tool_calls`：同一轮 content 里的 tool_use 分组——
  **串行组**（权限提示工具、hook 途径、非 parallel_safe）逐个按现状执行；
  **并行组**（parallel_safe 且无权限提示且项目未配置 PreToolUse 拒绝）
  用 `ThreadPoolExecutor(max_workers=4)` 执行。
- 事件顺序：先按原顺序 yield/emit 全部 tool_use；tool_result 按完成
  顺序发（并行组），串行组语义不变。`ctx`（ToolContext）共享只读。
- 配置：`max_workers` 读配置项（默认 4），`0/1` 退化为纯串行即现状。

## 4. M4-3 /readyz

新路由 `GET /readyz`（无鉴权，与 /health 同级）：

- storage 探针：data_dir 存在且可写（touch 临时文件）。
- sandbox 探针：容器后端可达（subprocess 后端恒 ready）。
- llm 探针：复用 /health 的 llm_configured 逻辑。

任一失败 → 503 + `{"status":"unready","checks":{...}}`；全过 → 200。
语义区分：/health=进程存活（轻），/readyz=可服务（带探针开销）。
探针结果缓存 5s 防高频打挂。

## 5. M4-4 指标补全

在现有 `MetricsRegistry`（server/metrics.py）注册新族并埋点：

| 指标 | 类型 | 标签 | 埋点位置 |
|---|---|---|---|
| `tool_calls_total` | counter | tool, outcome(success/failure) | loop `_execute_tool_calls` |
| `tool_duration_seconds` | histogram | tool | 同上 |
| `mcp_servers` | gauge（按状态拆 counter 更合规） | status(connected/failed) | mcp pool 连接/断开 |
| `mailbox_lock_wait_seconds` | histogram | — | teams MessageBus `_try_file_lock` |

指标实例获取路径与现有 anthropic_* 族一致（loop 侧已有
`_record_usage` 的注入模式可循）。

## 执行顺序与提交

1. `refactor(m3): split teams/__init__ into bus/protocol/spawner/convention`
2. `feat(m4): error classifier — classify_error + fail-fast retry + typed error events`
3. `feat(m4): conservative parallel tool execution (parallel_safe whitelist)`
4. `feat(m4): /readyz readiness probes`
5. `feat(m4): tool/MCP/lock-wait metrics`
6. `docs(roadmap): 勾选 M3-2/M4-1..4/M4-7 + 完成记录`

每项：先写测试（失败）→ 实现 → 全量 `pytest -q` 绿 → commit。
