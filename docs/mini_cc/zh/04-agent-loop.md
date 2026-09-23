# 04 — Agent 循环

[ < 上一章 ] [ 下一章 > ] · [English version](../en/04-agent-loop.md)

## 问题与动机

"Agent" 并不是一次 LLM 调用，而是一个**以轮次为单位的驱动循环**：不断发起模型请求、
执行模型要求调用的工具，并把结果回喂给模型，直到模型发出完成信号。`mini_cc` 把这个循环
实现成一个生成器，因此同一段代码可以同时服务三类截然不同的调用方：

1. **HTTP/SSE 客户端**把 yield 出来的事件当作实时流消费。
2. **SDK 内嵌者**在自己的进程里直接迭代生成器。
3. **子 agent**（`task` 工具、workflow dispatch）以受限的工具集再次进入循环。

因此同一个循环实例必须能被多个调用方安全驱动、必须在服务器崩溃后能恢复、并且必须在会话
增长超过上下文窗口时控制 token 用量。`mini_cc/core/loop.py` 就是处理这些关切的地方。本章
梳理循环的解剖结构：每轮事件契约、按会话的重入锁、分层压缩触发、错误恢复
（`MAX_RECOVERY_RETRIES`、模型回退、`output_style`），以及子 agent 如何作为新的子循环被
派生。

## 设计与原理

### 轮次契约

`AgentLoop.run(user_input)` 是一个生成器，yield 一组强类型事件流。完整的分类文档写在
`mini_cc/core/loop.py:4-9` 的文件头：

```
{"type": "text", "text": str}                       # 流式 assistant token
{"type": "tool_use", "name", "input", "id"}         # 模型要调用工具
{"type": "tool_result", "tool_use_id", "content"}   # 工具返回
{"type": "permission_request", "request_id", ...}   # 交互式审批门（第 06 章）
{"type": "todos_updated", "todos": [...]}           # 任务板状态变更
{"type": "cron_fired" / "background_notification"}  # 调度器注入
{"type": "retry", "reason": "429"|"529", "attempt"} # 正在退避重试
{"type": "max_tokens_escalation", "max_tokens"}     # 提升了预算
{"type": "cancelled"}                               # 用户中途点了 stop
{"type": "done"}                                    # 轮次干净结束
{"type": "error", "message"}                        # 不可恢复的失败
```

消费方应当把 `done` / `error` / `cancelled` 当作终止事件。循环保证每次 `run()` 调用
恰好发出一个终止事件。

### 每轮迭代的循环

```
   ┌──────────────────────────────────────────────────────────────┐
   │  AgentLoop._run_impl()  — 一个 user turn = N 次迭代          │
   │                                                              │
   │  while not _stop.is_set():                                   │
   │    1. 注入 cron_fired + background 通知                       │
   │    2. 连续 3 轮没用 todo_write 就提醒                         │
   │    3. _refresh_tools()  ← MCP 工具可即时出现                  │
   │    4. prepare_context()  ← 分层压缩（见下）                   │
   │    5. 组装系统提示词（或用 override）                          │
   │    6. 打开 provider 流（429/529 退避重试）                    │
   │    7. 实时转发 text_delta 事件                                │
   │    8. message_stop 时：append assistant blocks                │
   │    9. 若有 tool_use：dispatch，收集结果，再循环                │
   │       否则：yield done + 持久化 + return                       │
   └──────────────────────────────────────────────────────────────┘
```

循环由模型的 `stop_reason` 驱动。`tool_use` 会把工具结果作为新的 user turn 追加后再次
进入循环；`end_turn` 则退出。`max_tokens` 路径会升档一次（`DEFAULT_MAX_TOKENS` →
`ESCALATED_MAX_TOKENS`，`mini_cc/config.py:27-28`），若仍然撞上限，则发送
`CONTINUATION_PROMPT` 最多 `MAX_RECOVERY_RETRIES` 次后放弃（`mini_cc/core/loop.py:681-695`）。

### 保守的并行工具执行

带有 `parallel_safe` 标记的只读工具（`read_file`、`glob`、`grep`、`web_fetch`、
`web_search`）在同一轮 assistant 消息里连续出现时可以并发执行。这类批次的所有
`tool_use` 事件先全部发出，批内的 `tool_result` 事件随后按完成顺序（可能与提交顺序
不同）依次到达。并行批次之后的非并行工具会等批次结束后才运行，跨工具的先后顺序因此
保持不变。只要订阅了 `PreToolUse`/`PostToolUse` 钩子或配置了交互式权限审批，并行即被
关闭 —— 钩子观察到的永远是串行语义。线程池大小由 `MINI_CC_TOOL_WORKERS` 控制
（默认 4；小于 2 视为串行）。

### 按会话的重入锁

两个线程同时进入同一个 loop 的 `run()`，会在 `self.messages` 上竞争并损坏 transcript。
守卫是显式的：

```python
# mini_cc/core/loop.py:424-430
with self._running_lock:
    if self._running:
        raise RuntimeError(
            "AgentLoop.run() already in progress on this loop — "
            "concurrent calls would corrupt the transcript. ...")
    self._running = True
```

HTTP/SessionManager 路径已经按项目串行化，但 SDK 内嵌者如果自己起线程就会触发这个快速
失败。并行会话请用独立的 `AgentLoop`，绝不要在一个 loop 上跑两个线程。

### 分层压缩

`prepare_context()`（`mini_cc/core/compaction.py:129`）按顺序施加三道工序，破坏性依次
递增：

| 工序 | 做什么 | 触发条件 |
|------|--------|----------|
| `tool_result_budget` | 把比最近 3 条更早的 tool_result 内容截断到 2000 字符 | 总是 |
| `snip_compact` | 把最旧的带工具的 assistant turn 整体替换为占位符 | 仅当超过 `CONTEXT_LIMIT`（50k token） |
| `micro_compact` | 折叠尾部空的 user 消息 | 总是 |
| `compact_history` | 保留最后 6 条，其余摘要 | 仅当仍然超预算 |

当 provider 返回 "prompt too long" 错误时还会触发响应式回退：`compact_history` 跑一次
（由 `has_attempted_reactive_compact` 守卫），然后重试该迭代
（`mini_cc/core/loop.py:632-636`）。任何破坏性压缩之前，`_save_transcript` 会先把完整的
压缩前消息列表写盘，便于事后回放整段对话。

### 崩溃恢复与 transcript 修复

每轮结束后通过 `_persist()` 持久化状态。warm-load 时，
`repair_dangling_tool_uses()`（`mini_cc/core/loop.py:93`）修复两种否则会让 Anthropic API
返回 400 的损坏形态：

- **前向悬空**：尾部是带 `tool_use` 块但没有匹配 `tool_result` 的 assistant 消息 → 追加
  一条合成 user turn，把每个标记为 `"[interrupted by server restart]"`。
- **反向孤儿**（P1-9）：尾部是 user 消息，但其 `tool_result` 块引用的 `tool_use_id` 在前
  一条 assistant 消息里不存在 → 剥离孤儿块；若全部是孤儿则把整条消息替换为文本说明。

同样的部分持久化逻辑在 cancel 和流中途异常时也会运行（`loop.py:597-664`）：模型在中断前
产出的文本 + tool_use 会被保存为一条连贯的 assistant 消息，并合成匹配的 `[interrupted]`
tool_result，使下一轮的 API 调用看到配对的使用/结果。

`RecoveryState`（`mini_cc/core/recovery.py:14`）记录 `recovery_count`、`consecutive_529`、
`has_escalated`、`has_attempted_reactive_compact`、`current_model`，以及 `output_style`
（由 `/output-style` 设置的详略提示，被系统提示词构建器读取）。

### 子 agent 派生

`spawn_subagent()`（`mini_cc/core/subagent.py:36`）构造一个全新的 `AgentLoop`，使用冻结
且受限的工具集 —— 只有 `bash`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`
（`SUBAGENT_TOOL_NAMES`，line 23）。子循环以独立的 `subagent-<uuid8>` session_id 跑到完
成，工具调用硬上限 30 次，系统提示词末尾固定为 "Do not spawn more agents."。除非调用方传
`allow_mcp=True`（B10），否则排除 MCP 工具。父循环的 `on_event` 被转发进来，因此子 agent
的 `tool_use`/`tool_result` 活动会实时浮现在父循环的 SSE 流里。

## 操作与配置

### 环境变量（由 `AnthropicConfig.from_env` 读取，`mini_cc/config.py:109`）

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` / `MINI_CC_ANTHROPIC_API_KEY` | Anthropic SDK 凭证 |
| `ANTHROPIC_BASE_URL` / `MINI_CC_ANTHROPIC_BASE_URL` | Anthropic 兼容端点 |
| `MODEL_ID` | 主模型（默认 `claude-sonnet-4-6`） |
| `FALLBACK_MODEL_ID` | 连续 2 次 529 后切换到 |
| `LITELLM_API_KEY` / `OPENAI_API_KEY` | litellm 凭证（带前缀的模型走这条路） |
| `LITELLM_BASE_URL` / `OPENAI_BASE_URL` | OpenAI 兼容端点 |
| `TAVILY_API_KEY` | 启用基于 Tavily 的 `web_search` |
| `MINI_CC_MCP_SERVERS` | 自动连接的 MCP 服务器的 JSON 映射 |

### 循环调优常量（`mini_cc/config.py:27-35`）

| 常量 | 默认值 | 含义 |
|------|--------|------|
| `DEFAULT_MAX_TOKENS` | 8000 | 单次请求输出上限 |
| `ESCALATED_MAX_TOKENS` | 16000 | 第一次 `max_tokens` stop_reason 之后 |
| `MAX_RETRIES` | 3 | 打开流时 429/529 的重试次数 |
| `MAX_RECOVERY_RETRIES` | 2 | 升档后的 continuation prompt 次数 |
| `CONTEXT_LIMIT` | 50000 | 触发压缩的 token 估算阈值 |
| `KEEP_RECENT_TOOL_RESULTS` | 3 | 截断前保留完整内容的工具结果数 |

### Provider 选择

`select_provider(model, cfg)`（`mini_cc/core/llm.py:430`）按模型名前缀路由。`claude-*`
（无斜杠）→ `AnthropicProvider`；`LITELLM_PREFIXES` 里的任何前缀（`openai/`、`deepseek/`、
`qwen/`、`gemini/` 等）→ `LiteLLMProvider`。两者都 yield 同一种归一化的 `StreamEvent`
（`llm.py:38`），因此 `loop.py` 永远不按后端分支。

## 验证步骤

针对 `:8002` 上的后端运行。把 `$TID`、`$PID`、`$SID`、`$KEY` 替换为你的
tenant/project/session/api-key 值。

```bash
# 1. 为某个 session 打开 SSE 流
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -H "Content-Type: application/json" \
  -d '{"message":"用 bash 列出项目根目录下的文件。"}'

# 观察事件流：text 增量 → tool_use(bash) → tool_result → done

# 2. 通过请求超长输出触发 max_tokens 升档
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"写 5000 字关于洋流的内容。"}'
# 期望看到 {"type":"max_tokens_escalation","max_tokens":16000} 事件

# 3. 模拟崩溃重启后检查持久化的 transcript
ls $WORKSPACE/.mini_cc/sessions/$PID/$SID/
# 应当存在 messages.json + todos.json 且 JSON 合法

# 4. 通过 task 工具驱动子 agent
curl -N -H "Authorization: Bearer $KEY" \
  "http://localhost:8002/tenants/$TID/projects/$PID/sessions/$SID/send" \
  -d '{"message":"用 task 工具在 src/ 下 grep TODO 并总结。"}'
# 子 agent 的 tool_use/tool_result 事件应嵌套显示在父轮次下
```

## 常见坑与调试

1. **`RuntimeError: AgentLoop.run() already in progress`** —— 两个线程共用一个 loop。修复
   方式是按 session 拆 loop，而不是加更粗的锁。常见元凶是把用户消息丢到 worker 线程的
   SDK 内嵌者。
2. **崩溃恢复后 `400`** —— warm-load 修复没跑。请确认会话预热路径调用了
   `repair_dangling_tool_uses()`；症状是 `messages.json` 里有孤儿 `tool_result` 块。
3. **卡在 `max_tokens` 反复循环** —— 升档只触发一次，之后是
   `MAX_RECOVERY_RETRIES=2` 次 continuation prompt。若模型仍输出过长，循环会 yield
   `done` 并带截断内容。调高 `ESCALATED_MAX_TOKENS` 或拆分任务。
4. **压缩过早丢弃工具输出** —— `tool_result_budget` 会把比最近 3 条更早的 tool_result
   截断到 2000 字符。若 agent 需要更早的输出，让它重跑工具，而不是调高
   `KEEP_RECENT_TOOL_RESULTS`。
5. **429/529 风暴** —— SDK 内部重试两次；循环在打开流时再加 `MAX_RETRIES=3` 退避。连续
   2 次 529 后，若设置了 `FALLBACK_MODEL_ID` 则切换。没有 fallback 时，耗尽后抛异常，
   轮次以 `error` 事件结束。

## 延伸阅读

- 源码：`mini_cc/core/loop.py`、`mini_cc/core/recovery.py`、`mini_cc/core/compaction.py`、
  `mini_cc/core/llm.py`、`mini_cc/core/subagent.py`
- 姊妹章节：[05 — 工具](../zh/05-tools.md)、[06 — 权限](../zh/06-permissions.md)
- 关于 agent 轮次的概念框架，参见 `docs/{en,zh}/` 下的 `sNN-*` 系列。
