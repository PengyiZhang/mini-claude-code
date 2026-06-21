# Phase H:job 模式异步 turn(把 loop.run 从 HTTP handler 解耦)

## 背景

Phase A–G 已经覆盖了 agent 框架,Phase G 加了容器 sandbox 设计。当前
每个 `POST /sessions/{sid}/send` 都返回一个 `StreamingResponse`,其
async generator 通过 `sse_stream()` 把同步侧的 `SessionManager.send()`
迭代器在 `ThreadPoolExecutor` 里跑后桥接出来。能用,但是:

- HTTP 请求**全程占用** turn 的执行。如果 Anthropic API 慢(30 秒+),
  连接就干挂着;客户端没法断开重连,一断 turn 就 abort 了。
- 如果 worker 线程跑到一半死了,客户端会在 stream 中途收到 500 ——
  没有重放,没有 `job_id` 可以重试。
- 客户端没有"提交消息"和"消费结果"的解耦;UI 必须保持 SSE 通道,
  一断就丢 turn。

Phase H 加一个**可选的第二模式**做解耦:

- `POST /sessions/{sid}/send?mode=job` 立即返回 `{job_id}`。
- 客户端通过 `GET /jobs/{job_id}/events`(SSE)订阅,可自由断开重连,
  并能重放历史。
- 取消是显式的(`POST /jobs/{job_id}/cancel`),不是隐式的
  (SSE 断开)。
- Per-project FIFO 队列吸收突发(深度 8);队列满返回 429 +
  `Retry-After`。

默认 `mode=stream` 保持现有接口形态字节不变。现有 UI、e2e、admin 工具、
外部客户端都不需要改。

## 方案(brainstorm 锁定)

- **接口形态**:`?mode=job` query 参数。默认 `mode=stream` 逐字节保持
  Phase B 的 SSE 契约。
- **状态**:纯内存。Job 元数据持有到 server 重启;事件 ring buffer
  1000 条 LRU;终态后 5 分钟 TTL 保留事件 buffer(元数据继续留)。
- **取消**:仅显式 `POST /jobs/{job_id}/cancel`。断开
  `/jobs/{job_id}/events` **不**取消 —— 客户端可能在重连或切 tab。
- **排队**:per-project FIFO,默认深度 8(env 可调)。同 project 的
  job 串行;跨 project 并行。队列满 → HTTP 429 + `Retry-After: 1`。
- **worker 池**:复用 `sse_stream` 已有的 `ThreadPoolExecutor`。不引入
  新的线程管理。
- **重放**:晚到的 SSE 订阅者先从 offset 0 收缓冲事件,再追尾 live 事件,
  直到 job 到终态。

## 状态机

```
                ┌──────────┐
                │ queued   │
                └────┬─────┘
                     │ worker 拿到(project 锁已获)
                     ▼
                ┌──────────┐    POST /cancel
                │ running  │──────────────┐
                └────┬─────┘               ▼
                     │              ┌──────────┐
              turn   │              │cancelling│
              done   │              └────┬─────┘
                     ▼                   │ session.stop() 生效
                ┌──────────┐             ▼
                │  done    │        ┌──────────┐
                └──────────┘        │cancelled │
                                    └──────────┘

        exception / agent 错误
                ┌──────────┐
                │  failed  │
                └──────────┘
```

`queued → running` 转换发生在 worker 拿到 per-project 锁并开始消费
`SessionManager.send()` 时。`cancelling` 状态对外只是过渡标志;客户端
可以继续 stream 事件直到终态 `cancelled` 事件落地。

## 文件清单

### `mini_cc/server/jobs.py`(新,~200 行)

```python
@dataclass
class JobRecord:
    job_id: str                   # uuid4 hex,32 字符
    tenant_id: str
    project_id: str
    session_id: str
    user_input: str
    status: JobStatus             # queued|running|done|failed|cancelled|cancelling
    created_at: float             # time.time()
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # 事件 ring buffer;超过 MAX_EVENTS 时丢最老的。
    events: deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    # 新事件通知器,给 SSE 追尾用。asyncio.Event 不是线程安全的;
    # 这里用 threading.Condition + asyncio 桥(见 JobManager)。
    _cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False

MAX_EVENTS = 1000
EVENT_TTL_SECONDS = 300           # 终态后保留时长

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)


class JobManager:
    """JobRecord 的内存注册表 + 过期 buffer 清理。"""

    def __init__(self):
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def create(self, *, tid, pid, sid, user_input) -> JobRecord: ...
    def get(self, job_id) -> JobRecord | None: ...
    def list_for_session(self, tid, pid, sid) -> list[JobRecord]: ...

    def append_event(self, job_id, event: dict) -> None:
        """worker 线程消费 send() 时调用。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None: return
            job.events.append(event)
            with job._cond:
                job._cond.notify_all()

    def request_cancel(self, job_id) -> bool:
        """幂等。返回 True 表示 status 之前是非终态。"""

    def cleanup_expired_buffers(self) -> int:
        """丢掉终态超过 TTL 的事件 buffer。由周期 asyncio 任务调用。
        返回清理数量。"""
```

### `mini_cc/server/queue.py`(新,~80 行)

```python
class PerProjectJobQueue:
    """跟踪 per-(tid, pid) 的 pending job;超过深度拒绝。"""

    def __init__(self, max_depth: int = DEFAULT_DEPTH):
        self._depths: dict[tuple[str, str], int] = {}
        self._max = max_depth
        self._lock = threading.Lock()

    def try_enqueue(self, tid: str, pid: str) -> bool:
        """原子占一个槽位。满返回 False。"""
        with self._lock:
            key = (tid, pid)
            if self._depths.get(key, 0) >= self._max:
                return False
            self._depths[key] = self._depths.get(key, 0) + 1
            return True

    def release(self, tid: str, pid: str) -> None:
        """job 到终态时调用。"""
        with self._lock:
            key = (tid, pid)
            n = self._depths.get(key, 0)
            if n > 0:
                self._depths[key] = n - 1

    def depth(self, tid: str, pid: str) -> int:
        with self._lock:
            return self._depths.get((tid, pid), 0)
```

默认深度来自 `MINI_CC_JOB_QUEUE_DEPTH` env(默认 `8`)。

### `mini_cc/server/routes/sessions.py`(改,+40 行)

给 `send_message` 加 `mode` query 参数:

```python
@router.post("/{sid}/send")
def send_message(body: SendMessageRequest,
                 mode: str = Query("stream", regex="stream|job"),
                 sid=Path(...), pid=Path(...),
                 tid=Depends(check_rate_limit_scope("sessions:write")),
                 pm=Depends(get_pm), sm=Depends(get_sm),
                 request: Request = ...):
    ...
    if mode == "stream":
        # 现有 StreamingResponse 路径 —— 逐字节不变
        ...

    # mode == "job"
    jobs: JobManager = request.app.state.jobs
    queue: PerProjectJobQueue = request.app.state.job_queue

    if not queue.try_enqueue(tid, pid):
        raise RateLimited(retry_after=1,
                          details={"reason": "project job queue full"})

    job = jobs.create(tid=tid, pid=pid, sid=sid,
                      user_input=body.user_input)
    # 起 worker(复用 sse_stream 的 executor)
    asyncio.get_event_loop().run_in_executor(
        None, _run_job, jobs, queue, sm, pid, sid,
        body.user_input, job.job_id, tid)
    return JSONResponse(
        {"job_id": job.job_id, "status": job.status.value,
         "events_url": f"/tenants/{tid}/projects/{pid}/jobs/{job.job_id}/events"},
        status_code=202)
```

`_run_job(jobs, queue, sm, pid, sid, text, job_id, tid)`:

```python
def _run_job(...):
    job = jobs.get(job_id)
    job.started_at = time.time()
    job.status = JobStatus.RUNNING
    jobs.append_event(job_id, {"type": "job_started"})

    try:
        # try_lock 已经被 queue 的 per-project 串行化覆盖,
        # 但 sm.send() 内部仍会调 sm.try_lock;让它 raise Conflict
        # → 映射到 failed。
        for event in sm.send(pid, sid, text):
            if job.cancel_requested:
                # sm._get_session(pid, sid).stop() 已被 cancel 接口调用;
                # loop 正在收尾。状态保留,继续 drain 让 cancel 事件落地。
                pass
            jobs.append_event(job_id, event)
        job.status = (JobStatus.CANCELLED if job.cancel_requested
                      else JobStatus.DONE)
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = f"{type(e).__name__}: {e}"
        jobs.append_event(job_id, {"type": "error", "message": job.error})
    finally:
        job.finished_at = time.time()
        jobs.append_event(job_id, {
            "type": "job_finished", "status": job.status.value})
        queue.release(tid, pid)
```

### `mini_cc/server/routes/jobs.py`(新,~120 行)

```python
router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/jobs",
    tags=["jobs"],
)

@router.get("", response_model=list[JobOut])
def list_jobs(pid, sid: str | None = None,
              tid=Depends(require_scope("sessions:read")),
              request: Request = ...):
    """列出 project 的 job(可按 session 过滤)。"""
    jobs = request.app.state.jobs
    return [JobOut(**j.__dict__) for j in jobs.list_for_project(tid, pid, sid)]

@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id, tid=Depends(require_scope("sessions:read")),
            request: Request = ...):
    job = _require_job(request, tid, job_id)
    return JobOut(**job.__dict__)

@router.post("/{job_id}/cancel", status_code=204)
def cancel_job(job_id,
               tid=Depends(require_scope("sessions:write")),
               request: Request = ..., sm=Depends(get_sm), pm=Depends(get_pm)):
    job = _require_job(request, tid, job_id)
    if job.status.is_terminal:
        return                                # 幂等
    jobs = request.app.state.jobs
    jobs.request_cancel(job_id)
    job.status = JobStatus.CANCELLING
    # 通知运行中的 AgentLoop 在下一次 yield 退出。
    try:
        sm._get_session(job.project_id, job.session_id).stop()
    except KeyError:
        pass                                  # race;按 no-op 处理

@router.get("/{job_id}/events")
def job_events(job_id,
               tid=Depends(require_scope("sessions:read")),
               request: Request = ...):
    """SSE:从 offset 0 重放缓冲事件,然后追尾 live。"""
    job = _require_job(request, tid, job_id)
    return StreamingResponse(
        _sse_drain(request.app.state.jobs, job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

async def _sse_drain(jobs: JobManager, job_id: str) -> AsyncIterator[str]:
    """从 offset 0 重放,然后追尾到终态。"""
    offset = 0
    loop = asyncio.get_running_loop()
    job = jobs.get(job_id)
    while True:
        # 快照当前 offset 之后的事件
        events = list(job.events)[offset:]
        offset += len(events)
        for ev in events:
            yield f"data: {json.dumps(ev, default=str)}\n\n"
        if job.status.is_terminal and not events:
            yield "data: [DONE]\n\n"
            return
        # 等更多事件(或终态)
        await loop.run_in_executor(None, _wait_event, job, 1.0)
```

`_wait_event(job, timeout)` 阻塞在 `job._cond.wait(timeout)`,这样
async 侧可以 `await loop.run_in_executor` 而不空转。

### `mini_cc/server/schemas.py`(改,+20 行)

```python
class JobOut(BaseModel):
    job_id: str
    tenant_id: str
    project_id: str
    session_id: str
    status: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    cancel_requested: bool
```

### `mini_cc/server/app.py`(改,+15 行)

`build_app` 新增 `jobs: JobManager | None = None` 和
`job_queue: PerProjectJobQueue | None = None` kwarg。None 时懒构造默认
实例。两者都存到 `app.state`。

Lifespan 起一个周期清理任务:

```python
@asynccontextmanager
async def lifespan(app):
    cleanup_stop = asyncio.Event()
    async def _purge_loop():
        while not cleanup_stop.is_set():
            try:
                app.state.jobs.cleanup_expired_buffers()
            except Exception:
                pass
            try:
                await asyncio.wait_for(cleanup_stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
    task = asyncio.create_task(_purge_loop())
    yield
    cleanup_stop.set()
    await task
    # 后续 session + 容器清理继续
```

### `mini_cc/server/cli.py`(改,+5 行)

```python
queue_depth = int(os.environ.get("MINI_CC_JOB_QUEUE_DEPTH", "8"))
job_queue = PerProjectJobQueue(max_depth=queue_depth)
jobs = JobManager()
app = build_app(..., jobs=jobs, job_queue=job_queue)
```

### `tests/test_phaseH_jobs.py`(新,~250 行)

`FakeSessionManager` 复用 Phase B 测试的同形态,但记录 `send()` 调用,
允许测试注入脚本化的事件序列 + 取消行为。

测试:

- `POST /send?mode=job` 返回 202 + `job_id`;立即在
  `GET /jobs/{job_id}` 看到 `queued`。
- `mode=stream`(默认)逐字节不变 —— SSE body 与 Phase B 基线一致。
- worker 把 `sm.send()` 事件 drain 进 job buffer;之后连上的 SSE 客户端
  按序看到所有事件,接 `[DONE]`。
- 晚到的 SSE 订阅者(job 已终态)从 offset 0 重放 + 立即 `[DONE]`。
- `POST /jobs/{job_id}/cancel` 把 status 翻成 `cancelling` 并调
  `session.stop()`;worker drain 完剩余事件,最终落 `cancelled`。
- cancel 幂等(第二次调用仍 204)。
- 终态后再 cancel 是 no-op(仍 204)。
- 断开 SSE 订阅者**不**取消 job(通过断开 + 重连验证 —— 事件继续流)。
- 队列深度强制:快速向一个 project 提 8 个 job → 全部接受;第 9 个
  返回 429 + `Retry-After: 1`。
- 跨 project 并行:project A 提 4 个 + project B 提 4 个 → 全部并行
  (worker 数 > 1)。
- `cleanup_expired_buffers()` 丢掉超过 TTL 的事件 buffer,但保留
  元数据;`GET /jobs/{job_id}` 仍返回记录。
- tenant1 的 job 对 tenant2 不可见(404)。
- Job scope 检查:`admin:read` key 能列任何 session 的 job,但
  `sessions:read` key 只能列自己 session 的 job(由 `_require_job`
  的 tenant 检查 + 额外的 session-id 检查强制)。

### `tests/test_phaseH_http.py`(新,~120 行)

通过 `TestClient(build_app(...))` + stub Anthropic client 端到端:

- 提交 job → 轮询 `GET /jobs/{job_id}` → status 正确转换
  `queued → running → done`,带时间戳。
- 提交 job,立即断开 SSE,用相同 `job_id` 重连,获得完整事件流。
- Job 失败(stub 抛错)→ status `failed`,`error` 已填,事件中包含
  最终的 `error` 事件。

### `mini_cc/README.md` + `.zh.md`(改)

HTTP endpoints 下新增"Async job mode(Phase H)"小节:

- `POST /send?mode=job` 返回 202 + `{job_id, events_url}`。
- `GET /jobs/{job_id}/events` SSE 流(重放 + 追尾)。
- `POST /jobs/{job_id}/cancel` 幂等取消。
- `GET /projects/{pid}/jobs` 列出 job。
- 队列深度 env `MINI_CC_JOB_QUEUE_DEPTH`。
- 迁移指引:现有客户端可以留在 `mode=stream`;需要容错的客户端切到
  `mode=job` + 可重连 SSE。

## 验证

```bash
# 1. 单元
python -m pytest tests/test_phaseH_jobs.py -v
python -m pytest tests/test_phaseH_http.py -v

# 2. 全量(预期 ~460 通过)
python -m pytest tests/ -q

# 3. 手动:stream 模式回归
curl -N -X POST .../sessions/$SID/send \
     -H "Authorization: Bearer $KEY" \
     -d '{"user_input":"hi"}' | head   # 行为不变

# 4. 手动:job 模式生命周期
JOB=$(curl -sX POST ".../sessions/$SID/send?mode=job" \
        -H "Authorization: Bearer $KEY" \
        -d '{"user_input":"hi"}' | jq -r .job_id)
curl -N ".../jobs/$JOB/events" -H "Authorization: Bearer $KEY"
# 断开,重连,观察重放
curl -X POST ".../jobs/$JOB/cancel" -H "Authorization: Bearer $KEY"

# 5. 手动:队列深度
for i in {1..9}; do
  curl -X POST ".../sessions/$SID/send?mode=job" \
       -d '{"user_input":"slow"}' ... &
done
# 第 9 个应该 429
```

## Phase H 范围外

- **持久化 job 状态。** 纯内存。Server 重启即丢。跨进程持久化需要
  Redis/NATS,不在范围。
- **跨机器 job 分发。** 单进程 worker 池对 dev 框架规模够用。
- **Per-session 或 per-tenant 队列限制。** 只有 per-project。
- **Job 优先级。** 只有 FIFO。
- **Job 管理 Web UI。** 现有 chat UI 留在 `mode=stream`。job 仪表盘
  页面延后 —— API 稳定后,Phase F 的 `useAdmin` store 模式可以承载。
- **Per-job 超时。** 遵守 AgentLoop 内部已有的 per-tool 超时;没有
  job 级 wall clock。需要再加。
- **事件格式变更。** `SendEvent` 形态逐字复用;客户端除了
  `job_started` / `job_finished` 生命周期标记(跟现有 `done`/`error`
  类似对待)之外,不需要新事件类型。
- **Server-Sent Events 的替代(WebSocket)。** 继续 SSE。
