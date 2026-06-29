[ < [01](01-overview.md) ] [ **03** > ] · [English version](../en/02-storage-projects-sessions.md)

# 02 — 存储、项目与会话

> 三件事在概念章里通常被一笔带过:**状态落在盘上哪里**、**一个项目由什么组成**、
> 一轮对话**怎么串行**。本章把这三件事拆到底,并指出几个生产里会踩的坑:
> 原子写、sessions 索引、per-project 锁、冷热会话续接。

---

## 问题与动机

把 Claude Code 从单机脚本搬到多租户后端,状态管理是第一个崩的地方:

- **崩溃恢复**:server 在 turn 中途被 OOM kill,下次启动怎么把对话续上?
  Anthropic 的 messages API 要求 `tool_use` 块后面必须紧跟对应的
  `tool_result`,否则下次 stream 会 4xx。如果磁盘上最后一条是孤立的
  `tool_use`,模型就被卡死了。
- **并发正确性**:两个 HTTP 请求同时 send 到同一项目,LLM 是有状态调用,
  messages 文件会被互相覆盖。但不同项目应该完全并行——不能一把全局锁。
- **租户隔离的物理保证**:两个租户用同一个 `project_id` 不能共享 workspace
  或 storage 子目录,否则 messages 泄漏。
- **可观测的 session 列表**:Web UI 要展示「哪些 session 在盘上、哪些在内存」,
  不能每次都扫 `messages/` 目录(几千个 session 时太慢)。
- **跨 session 检索**:用户搜「我之前在哪说过 deploy?」——纯子串扫描就够,
  不必上 sqlite FTS,但要预留换实现的口子。

mini_cc 的回答是三个相互咬合的抽象:`Storage` Protocol(可换后端)、
`ProjectManager`(装配 + 缓存 + 目录布局)、`SessionManager`(冷热会话 + per-project
串行锁)。它们都不依赖 HTTP 层,SDK 直接用也是同一套语义。

---

## 设计与原理

### 1. Storage Protocol —— 可换后端

`mini_cc/storage/base.py:51` 定义了一个 Protocol,所有方法都 key 在
`project_id` 上,实现必须按项目隔离:

```python
# mini_cc/storage/base.py:51
class Storage(Protocol):
    def load_messages(self, project_id, session_id) -> list[dict]: ...
    def save_messages(self, project_id, session_id, msgs) -> None: ...
    def list_sessions(self, project_id) -> list[SessionMeta]: ...
    def save_session_meta(self, project_id, meta: SessionMeta) -> None: ...
    def search_messages(self, project_id, query, limit=20) -> list["SearchHit"]: ...
    def write_transcript(self, project_id, msgs) -> None: ...
    # 还有 todos / tasks / cron / memory / workflows / tool_results / events
```

默认实现 `FSStorage`(`mini_cc/storage/fs.py:48`)把每个项目的状态放在
`<state_root>/<project_id>/` 下:

```
<state_root>/<project_id>/
  messages/<session_id>.json       ← 一个 session 的全部 messages
  todos/<session_id>.json
  tasks/<task_id>.json
  memory/MEMORY.md
  cron/jobs.json
  sessions/index.json              ← SessionMeta 索引(避免每次扫 messages/)
  sessions/<session_id>.events.jsonl  ← SSE 事件流(给 Last-Event-Id replay 用)
  transcripts/transcript_<ts>.jsonl
  tool_results/<tool_use_id>.txt   ← 大输出落盘,context 里只留引用
  workflows/<wf_id>.json
  workflow_defs/<def_id>.json      ← Workflow V2:定义 + run 分开存
  workflow_runs/<run_id>.json
```

三个细节值得记住:

- **原子写**。所有 JSON 写都走 `_atomic_write_json`
  (`mini_cc/storage/fs.py:146`):先写 `tempfile.mkstemp` 临时文件,再
  `os.replace` 覆盖目标。`os.replace` 在 POSIX 和 Windows 上都是原子的,所以
  读到的永远是完整的旧版本或完整的新版本,不会读到半截 JSON。
- ** corruption 显式报错**。`load_messages` 解析失败抛 `StorageCorruptionError`
  (`storage/fs.py:37`),而不是返回 `[]`。原因写在注释里:corrupted 文件如果
  被当成「空 session」,下次 save 会用单条消息覆盖它,用户对话无信号地丢失。
- **跨 session 检索**。`search_messages`(`storage/fs.py:189`)是纯线性扫描 +
  80 字符窗口 snippet。注释明说「adequate for moderate scale;swap to sqlite
  FTS5 later without changing the call site」——预留了换后端的口子。

### 2. ProjectManager —— 装配 + 缓存 + 目录布局

`ProjectManager`(`mini_cc/projects/manager.py:123`)负责:

- **创建 / 列举 / 删除项目**,带 ID 校验 `[A-Za-z0-9_-]+`
  (`_validate_project_id`,`manager.py:44`)。这条关掉了 `delete` 里
  `shutil.rmtree` 的路径穿越漏洞。
- **装配**(`_assemble`,`manager.py:278`):一次性 wire 一个项目的全部依赖
  (sandbox、storage、skills、scheduler、mcp、teams、hooks、permissions)。
- **缓存装配结果**(`get`,`manager.py:225`),按配置文件 mtime 失效。这点很
  关键:HTTP 路由每次 `pm.get()`(每个请求都调)如果都重装,远程 MCP server
  连接要 ~2s,还会泄漏子进程。

装配缓存的失效规则在 `_config_signature`(`manager.py:152`):

```python
# mini_cc/projects/manager.py:152
def _config_signature(self, tenant_id, workspace):
    """对每层 .mini_cc/ 目录里的 .mcp.json / mcp.toml / permissions.toml
    做 stat,把 (path, mtime_ns, size) 打包成 tuple。mtime 一变就失效。"""
```

租户隔离的目录布局在 `mini_cc/projects/layout.py:1`:

```
<data>/tenants/<tid>/projects/<pid>/{workspace,.state,meta.json}
<data>/tenants/<tid>/.storage/<pid>/       ← 租户级 storage 根
```

**`create()` 故意不 prime 缓存**(`manager.py:215` 注释)。原因:create 返回时
调用方可能还在配置 workspace(比如写 `.mini_cc/permissions.toml`)。此时 prime
会把一个半配置好的 Project 钉进缓存,后续 get() 返回的就是陈旧对象。第一次
`get()` 才在 workspace 配置完后惰性装配并缓存。

### 3. SessionManager —— 冷热会话 + per-project 锁

`SessionManager`(`mini_cc/session/manager.py:51`)是 SDK 核心和 HTTP 之间的
桥。它持有 warm 的 `AgentLoop` 字典,key 是 `(project_id, session_id)`。

**冷热会话**:一个 session **cold** 是指它只在磁盘上、当前进程没有对应
`AgentLoop`;**warm** 是指 AgentLoop 已把对话历史载入内存。三种续接路径:

```
                  POST /sessions/{sid}/send
                            │
                            ▼
                  _ensure_warm(project_id, sid)
                            │
                ┌───────────┴────────────┐
                │ in self._sessions?     │
                └───────┬────────────────┘
                  no    │      yes → 直接返回 warm session
                        ▼
                  storage.list_sessions(project_id) 含 sid?
                  no → KeyError → 路由层 404
                        │ yes
                        ▼
                  _warm(project, sid):
                    AgentLoop(ref, sid) 重建
                    repair_dangling_tool_uses(messages)  ← 崩溃恢复
                    若有修复则 save_messages 落盘
```

**崩溃恢复的关键**:`repair_dangling_tool_uses`(`core/loop.py`)检查 transcript
尾部是否有「assistant 消息带 `tool_use` 但没有对应 `tool_result`」。如果有,
追加一条合成的 user turn,每个悬挂 id 补一条
`tool_result` 内容为 `[interrupted by server restart]`、`is_error: true`。
这样既不丢上文,模型也能继续推进——Anthropic messages API 不会因为缺
tool_result 而 4xx。

**per-project 串行锁**(`session/manager.py:183`):

```python
# mini_cc/session/manager.py:183
def send(self, project_id, session_id, user_input):
    sess = self._ensure_warm(project_id, session_id)
    with self._lock_for(project_id):       # per-project RLock
        for ev in sess.loop.run(user_input):
            yield ev
```

`_lock_for` 按项目 id 维护一把 `threading.RLock`(惰性创建,`manager.py:58`)。
不同项目并行;同一项目串行。HTTP 层通过 `try_lock()`(`manager.py:169`)暴露
这一点——第二个并发 send 探到锁被持有,直接返回 409 `project_busy`,**不阻塞
HTTP worker 线程**。

**幂等 start**:`start_session`(`manager.py:64`)如果传入已存在的
`session_id`,返回 200(re-warm)而非 409。适用于「存在就打开,不存在就创建」。

### 4. Project 模板与 v0 迁移

`projects/templates.py:1` 实现项目模板:一个目录 `templates/<name>/`,里面有
`template.json`(元数据)+ 任意种子文件。`apply_template`(`templates.py:73`)
把它们 copytree 到新 workspace。操作员可通过 `MINI_CC_TEMPLATES_DIR` 放外部
pack。

老布局(扁平 `projects/<pid>/`)到新布局(`<data>/tenants/<tid>/projects/<pid>/`)
的一次性迁移在 `projects/migrate_v0_tenant_layout.py:1`。脚本默认 dry-run,
`--apply` 才真的 move。幂等:apply 后再跑是 no-op。

---

## 操作与配置

### 关键文件位置

| 关注点 | 路径 |
|--------|------|
| 一个 session 的 messages | `<data>/tenants/<tid>/.storage/<pid>/messages/<sid>.json` |
| sessions 索引 | `<data>/tenants/<tid>/.storage/<pid>/sessions/index.json` |
| 项目元数据 | `<data>/tenants/<tid>/projects/<pid>/meta.json` |
| key registry | `<data>/keys.json` |
| SSE 事件日志 | `<data>/tenants/<tid>/.storage/<pid>/sessions/<sid>.events.jsonl` |

### 相关环境变量

| 变量 | 用途 |
|------|------|
| `MINI_CC_DATA_DIR` | 整个 `<data>` 根 |
| `MINI_CC_TEMPLATES_DIR` | 外部模板 pack 根(覆盖包内 `templates/`) |

### HTTP 端点(本章相关)

| 方法 | 路径 | 行为 |
|------|------|------|
| `POST` | `/tenants/{tid}/projects` | 建;已存在 409 |
| `GET` | `/tenants/{tid}/projects/{pid}/sessions` | 列出 SessionMeta(含 `in_memory`) |
| `POST` | `/tenants/{tid}/projects/{pid}/sessions/{sid}/resume` | warm 一个冷会话,幂等 |
| `DELETE` | `/tenants/{tid}/projects/{pid}/sessions/{sid}` | 停止 + 注销 + 删盘上元数据 |

---

## 验证步骤

```bash
# 假定后端在 :8002,$KEY 是 my_tenant 的 key
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"project_id":"s2","display_name":"Storage demo"}' >/dev/null

curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions \
     -H "Authorization: Bearer $KEY" -d '{"session_id":"sx"}' >/dev/null

# 发一轮,产生 messages
curl -N -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"hello"}' | head -5

# 1. 看盘上 messages 文件(safe_session 把非字母数字字符替换成 _)
ls $PWD/mini_cc_data/tenants/my_tenant/.storage/s2/messages/
# → sx.json

# 2. 看 sessions 索引 —— message_count 应该 > 0
cat $PWD/mini_cc_data/tenants/my_tenant/.storage/s2/sessions/index.json | python -m json.tool

# 3. 重启 server 后,warm 探针:
curl -s -X POST http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/resume \
     -H "Authorization: Bearer $KEY" | python -m json.tool
# → {"session_id":"sx","message_count":N,...}

# 4. 并发 send 到同一项目 → 第二个 409 project_busy
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
     http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"concurrent 1"}' &  # 故意不 wait
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
     http://127.0.0.1:8002/tenants/my_tenant/projects/s2/sessions/sx/send \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"user_input":"concurrent 2"}'
# → 一个 200,一个 409

# 5. v0 → tenant 布局迁移 dry-run(对老数据目录)
python -m mini_cc.projects.migrate_v0_tenant_layout ./legacy_data
```

```bash
# 单元测试:FSStorage 原子写 + corruption 报错 + sessions 索引
python -m pytest tests/test_storage_fs.py -q
```

---

## 常见坑与调试

1. **`StorageCorruptionError` 当成「session 是空的」处理。** 这是反模式:
   corrupted 文件 ≠ 空 session。`load_messages` 显式抛错就是为了让你看到磁盘
   损坏,而不是静默覆盖。看到这个错先检查是不是写了一半进程被 kill。
2. **改 `permissions.toml` 后行为没变。** 装配缓存按 mtime 失效,但只在
   `get()` 时检查;`create()` 返回的 Project 不在缓存里。手动
   `pm.invalidate(pid, tenant_id=tid)` 强制重装。
3. **session 列表慢。** `list_sessions` 读 `sessions/index.json`,不扫
   `messages/`。如果某个项目是「索引特性之前创建的」,首次读会从
   `messages/*.json` 懒重建索引(`storage/fs.py:112`)。重建是一次性的,之后
   `save_messages` 会自动 upsert 索引。
4. **删了 session 但 `messages/<sid>.json` 还在。** 用
   `storage.delete_session`(而非只删索引),它会同时删 messages、todos、
   events log 和索引项。`SessionManager.remove` 走的就是这条路径。
5. **跨租户删除歧义。** `pm.delete(pid)` 不带 `tenant_id` 时,如果同一个 pid
   在多个租户下都存在,会抛 `ValueError("project_id ambiguous")`。HTTP 路由
   总是带 tid,不会碰到;SDK 直用要小心。

---

## 延伸阅读

- 兄弟章:[01 — 总览与架构](01-overview.md) · [03 — 三层沙箱](03-sandbox.md)
- 概念章:[s06 — Context Compaction](../../zh/s06-context-compact.md)
  (transcript 快照依赖 `write_transcript`)、
  [s07 — Task System](../../zh/s07-task-system.md)(task 持久化在
  `tasks/<id>.json`)
- 源码:`mini_cc/storage/fs.py`、`mini_cc/projects/manager.py`、
  `mini_cc/session/manager.py`、`mini_cc/projects/layout.py`
- 进阶:`docs/mini_cc/stateful-repl.md`(REPL state 持久化用到 storage)
