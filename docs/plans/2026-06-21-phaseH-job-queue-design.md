# Phase H: job-based async turns (decouple loop.run from HTTP handler)

## Context

Phases A–G cover the agent framework through Phase G's container
sandbox design. Every `POST /sessions/{sid}/send` today returns a
`StreamingResponse` whose async generator is bridged from the sync
`SessionManager.send()` iterator by `sse_stream()` running the sync
side in a `ThreadPoolExecutor`. That works, but:

- The HTTP request is **occupied** for the full duration of the turn.
  If the Anthropic API is slow (30 s+) the connection just hangs; the
  client can't disconnect and reconnect without aborting the turn.
- If the worker thread dies mid-iteration, the client gets a 500 mid
  stream — no replay, no `job_id` to retry.
- There is no client-side decoupling of "submit message" from "consume
  results"; UIs must keep the SSE pipe open or lose the turn.

Phase H adds an **opt-in second mode** that decouples them:

- `POST /sessions/{sid}/send?mode=job` returns immediately with
  `{job_id}`.
- Client subscribes via `GET /jobs/{job_id}/events` (SSE), can
  disconnect/reconnect freely, and can replay history.
- Cancellation is explicit (`POST /jobs/{job_id}/cancel`) instead of
  implicit (SSE disconnect).
- Per-project FIFO queue absorbs bursts (depth 8); queue-full returns
  429 + `Retry-After`.

Default `mode=stream` keeps the existing wire shape byte-for-byte.
Existing UI, e2e, admin tools, and external clients don't need to
change.

## Approach (locked via brainstorm)

- **Wire shape**: `?mode=job` query param. Default `mode=stream`
  preserves the Phase B SSE contract verbatim.
- **State**: pure in-memory. Job metadata retained until server
  restart; event ring buffer 1000 entries LRU; post-terminal 5 min
  TTL on the event buffer (metadata stays).
- **Cancellation**: explicit `POST /jobs/{job_id}/cancel` only.
  Disconnecting `/jobs/{job_id}/events` does **not** cancel — the
  client may be reconnecting or switching tabs.
- **Queueing**: per-project FIFO, depth 8 default (env-tunable).
  Same-project jobs serialize; cross-project parallelism preserved.
  Queue full → HTTP 429 with `Retry-After: 1`.
- **Worker pool**: reuse the existing `ThreadPoolExecutor` that
  `sse_stream` already uses. No new thread management.
- **Replay**: late SSE subscribers receive buffered events from offset
  0 first, then tail live events until the job reaches a terminal
  state.

## State machine

```
                ┌──────────┐
                │ queued   │
                └────┬─────┘
                     │ worker picks up (project lock acquired)
                     ▼
                ┌──────────┐    POST /cancel
                │ running  │──────────────┐
                └────┬─────┘               ▼
                     │              ┌──────────┐
              turn   │              │cancelling│
              done   │              └────┬─────┘
                     ▼                   │ session.stop() takes effect
                ┌──────────┐             ▼
                │  done    │        ┌──────────┐
                └──────────┘        │cancelled │
                                    └──────────┘

        exception / agent error
                ┌──────────┐
                │  failed  │
                └──────────┘
```

`queued → running` transition fires when the worker acquires the
per-project lock and starts draining `SessionManager.send()`. The
`cancelling` state is observed externally only as a transitional
flag; clients can keep streaming events until the terminal
`cancelled` event lands.

## File-by-file

### `mini_cc/server/jobs.py` (new, ~200 lines)

```python
@dataclass
class JobRecord:
    job_id: str                   # uuid4 hex, 32 chars
    tenant_id: str
    project_id: str
    session_id: str
    user_input: str
    status: JobStatus             # queued|running|done|failed|cancelled|cancelling
    created_at: float             # time.time()
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # Event ring buffer; LRU evict oldest beyond MAX_EVENTS.
    events: deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    # New-event notifier for SSE tail. asyncio.Event isn't thread-safe;
    # we use a threading.Condition + an asyncio bridge (see JobManager).
    _cond: threading.Condition = field(default_factory=threading.Condition)
    cancel_requested: bool = False

MAX_EVENTS = 1000
EVENT_TTL_SECONDS = 300           # post-terminal retention

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
    """In-memory registry of JobRecords + cleanup of stale buffers."""

    def __init__(self):
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def create(self, *, tid, pid, sid, user_input) -> JobRecord: ...
    def get(self, job_id) -> JobRecord | None: ...
    def list_for_session(self, tid, pid, sid) -> list[JobRecord]: ...

    def append_event(self, job_id, event: dict) -> None:
        """Called by the worker thread as it drains send()."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None: return
            job.events.append(event)
            with job._cond:
                job._cond.notify_all()

    def request_cancel(self, job_id) -> bool:
        """Idempotent. Returns True if status was non-terminal."""

    def cleanup_expired_buffers(self) -> int:
        """Drop event buffers of jobs that have been terminal > TTL.
        Called by the periodic asyncio task. Returns count purged."""
```

### `mini_cc/server/queue.py` (new, ~80 lines)

```python
class PerProjectJobQueue:
    """Tracks pending jobs per (tid, pid); rejects at depth."""

    def __init__(self, max_depth: int = DEFAULT_DEPTH):
        self._depths: dict[tuple[str, str], int] = {}
        self._max = max_depth
        self._lock = threading.Lock()

    def try_enqueue(self, tid: str, pid: str) -> bool:
        """Atomically reserve a slot. False if full."""
        with self._lock:
            key = (tid, pid)
            if self._depths.get(key, 0) >= self._max:
                return False
            self._depths[key] = self._depths.get(key, 0) + 1
            return True

    def release(self, tid: str, pid: str) -> None:
        """Called when a job reaches terminal state."""
        with self._lock:
            key = (tid, pid)
            n = self._depths.get(key, 0)
            if n > 0:
                self._depths[key] = n - 1

    def depth(self, tid: str, pid: str) -> int:
        with self._lock:
            return self._depths.get((tid, pid), 0)
```

Default depth from `MINI_CC_JOB_QUEUE_DEPTH` env (default `8`).

### `mini_cc/server/routes/sessions.py` (modify, +40 lines)

Add a `mode` query param to `send_message`:

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
        # existing StreamingResponse path — byte-for-byte unchanged
        ...

    # mode == "job"
    jobs: JobManager = request.app.state.jobs
    queue: PerProjectJobQueue = request.app.state.job_queue

    if not queue.try_enqueue(tid, pid):
        raise RateLimited(retry_after=1,
                          details={"reason": "project job queue full"})

    job = jobs.create(tid=tid, pid=pid, sid=sid,
                      user_input=body.user_input)
    # Spawn worker (reuses sse_stream's executor)
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
        # try_lock would already be enforced via the queue's per-project
        # serialization, but sm.send() still calls sm.try_lock internally;
        # we let it raise Conflict → mapped to failed.
        for event in sm.send(pid, sid, text):
            if job.cancel_requested:
                # sm._get_session(pid, sid).stop() was already called
                # by cancel endpoint; loop is winding down. Mark
                # but keep draining so the cancel event lands.
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

### `mini_cc/server/routes/jobs.py` (new, ~120 lines)

```python
router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/jobs",
    tags=["jobs"],
)

@router.get("", response_model=list[JobOut])
def list_jobs(pid, sid: str | None = None,
              tid=Depends(require_scope("sessions:read")),
              request: Request = ...):
    """List jobs for a project (optionally filtered by session)."""
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
        return                                # idempotent
    jobs = request.app.state.jobs
    jobs.request_cancel(job_id)
    job.status = JobStatus.CANCELLING
    # Signal the running AgentLoop to exit at the next yield.
    try:
        sm._get_session(job.project_id, job.session_id).stop()
    except KeyError:
        pass                                  # race; treat as no-op

@router.get("/{job_id}/events")
def job_events(job_id,
               tid=Depends(require_scope("sessions:read")),
               request: Request = ...):
    """SSE: replay buffered events from offset 0 then tail live."""
    job = _require_job(request, tid, job_id)
    return StreamingResponse(
        _sse_drain(request.app.state.jobs, job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

async def _sse_drain(jobs: JobManager, job_id: str) -> AsyncIterator[str]:
    """Replay events from offset 0, then tail until terminal."""
    offset = 0
    loop = asyncio.get_running_loop()
    job = jobs.get(job_id)
    while True:
        # Snapshot events from current offset
        events = list(job.events)[offset:]
        offset += len(events)
        for ev in events:
            yield f"data: {json.dumps(ev, default=str)}\n\n"
        if job.status.is_terminal and not events:
            yield "data: [DONE]\n\n"
            return
        # Wait for more events (or terminal)
        await loop.run_in_executor(None, _wait_event, job, 1.0)
```

`_wait_event(job, timeout)` blocks on `job._cond.wait(timeout)` so the
async side can `await loop.run_in_executor` without busy-spinning.

### `mini_cc/server/schemas.py` (modify, +20 lines)

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

### `mini_cc/server/app.py` (modify, +15 lines)

`build_app` gains `jobs: JobManager | None = None` and
`job_queue: PerProjectJobQueue | None = None` kwargs. If `None`, lazy
defaults are constructed. Both stored on `app.state`.

Lifespan spawns a periodic cleanup task:

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
    # existing session + container cleanup follows
```

### `mini_cc/server/cli.py` (modify, +5 lines)

```python
queue_depth = int(os.environ.get("MINI_CC_JOB_QUEUE_DEPTH", "8"))
job_queue = PerProjectJobQueue(max_depth=queue_depth)
jobs = JobManager()
app = build_app(..., jobs=jobs, job_queue=job_queue)
```

### `tests/test_phaseH_jobs.py` (new, ~250 lines)

`FakeSessionManager` reuses the same shape as Phase B's tests but
records `send()` calls and lets the test inject a scripted event
sequence + cancellation behavior.

Tests:

- `POST /send?mode=job` returns 202 with `job_id`; job visible in
  `GET /jobs/{job_id}` as `queued` immediately.
- `mode=stream` (default) is byte-for-byte unchanged — same SSE body
  as Phase B baseline.
- Worker drains `sm.send()` events into the job's buffer; SSE client
  connecting after the fact sees all events in order followed by
  `[DONE]`.
- Late SSE subscriber (job already terminal) gets replay from offset 0
  + immediate `[DONE]`.
- `POST /jobs/{job_id}/cancel` flips status to `cancelling` and calls
  `session.stop()`; the worker drains remaining events and finalizes
  as `cancelled`.
- Cancel is idempotent (second call still 204).
- Cancel after terminal is a no-op (still 204).
- Disconnecting the SSE subscriber does **not** cancel the job (verify
  by disconnecting and reconnecting — events still flow).
- Queue depth enforcement: submit 8 jobs to one project quickly → all
  accepted; 9th returns 429 with `Retry-After: 1`.
- Cross-project parallel: 4 jobs to project A + 4 to project B → all
  run in parallel (worker count > 1).
- `cleanup_expired_buffers()` drops event buffers older than TTL but
  keeps metadata; `GET /jobs/{job_id}` still returns the record.
- Job for tenant1 is not visible to tenant2 (404).
- Job scope check: `admin:read` key can list any session's jobs but
  `sessions:read` key can only list its own session's jobs (this is
  enforced via `_require_job` tenant check + an additional
  session-id check).

### `tests/test_phaseH_http.py` (new, ~120 lines)

End-to-end through `TestClient(build_app(...))` with a stub Anthropic
client:

- Submit job → poll `GET /jobs/{job_id}` → status transitions
  `queued → running → done` correctly with timestamps.
- Submit job, immediately disconnect SSE, reconnect with same
  `job_id`, get full event stream.
- Job fails (stub raises) → status `failed`, `error` populated,
  events include a final `error` event.

### `mini_cc/README.md` + `.zh.md` (modify)

New "Async job mode (Phase H)" subsection under HTTP endpoints:

- `POST /send?mode=job` returns 202 + `{job_id, events_url}`.
- `GET /jobs/{job_id}/events` SSE stream (replay + tail).
- `POST /jobs/{job_id}/cancel` idempotent cancellation.
- `GET /projects/{pid}/jobs` lists jobs.
- Queue depth env var `MINI_CC_JOB_QUEUE_DEPTH`.
- Migration guidance: existing clients can stay on `mode=stream`;
  for crash-tolerant clients, switch to `mode=job` + reconnectable
  SSE.

## Verification

```bash
# 1. Unit
python -m pytest tests/test_phaseH_jobs.py -v
python -m pytest tests/test_phaseH_http.py -v

# 2. Full suite (expect ~460 passing)
python -m pytest tests/ -q

# 3. Manual: stream-mode regression
curl -N -X POST .../sessions/$SID/send \
     -H "Authorization: Bearer $KEY" \
     -d '{"user_input":"hi"}' | head   # unchanged behavior

# 4. Manual: job mode lifecycle
JOB=$(curl -sX POST ".../sessions/$SID/send?mode=job" \
        -H "Authorization: Bearer $KEY" \
        -d '{"user_input":"hi"}' | jq -r .job_id)
curl -N ".../jobs/$JOB/events" -H "Authorization: Bearer $KEY"
# disconnect, reconnect, observe replay
curl -X POST ".../jobs/$JOB/cancel" -H "Authorization: Bearer $KEY"

# 5. Manual: queue depth
for i in {1..9}; do
  curl -X POST ".../sessions/$SID/send?mode=job" \
       -d '{"user_input":"slow"}' ... &
done
# 9th should 429
```

## Out of scope for Phase H

- **Persistent job state.** In-memory only. Jobs vanish on server
  restart. Cross-process persistence needs Redis/NATS, not in scope.
- **Cross-machine job distribution.** Single-process worker pool
  suffices for the dev framework scale.
- **Per-session or per-tenant queue limits.** Only per-project.
- **Job priority.** FIFO only.
- **Web UI for job management.** The existing chat UI stays on
  `mode=stream`. A job-dashboard page is deferred — once the API is
  stable, the same `useAdmin` store pattern from Phase F can carry it.
- **Per-job timeout.** Honors the existing per-tool timeout inside
  AgentLoop; no job-level wall clock. If needed, add later.
- **Event format change.** `SendEvent` shapes are reused verbatim;
  clients don't need new event types beyond `job_started` /
  `job_finished` lifecycle markers (treated like existing
  `done`/`error`).
- **Server-Sent Events alternative (WebSocket).** SSE stays.
