[ < [07](07-http-server.md) ] [ [09](09-auth.md) > ] · [中文版本](../zh/08-sse-streaming.md)

# 08 — SSE Streaming & Resume

> `/send` is the only streaming endpoint. It wraps a *synchronous*
> `SessionManager.send()` iterator in a worker thread and bridges it to
> an async SSE response through a bounded `asyncio.Queue`. This chapter
> covers the wire format, the `Last-Event-Id` replay protocol, the B8
> "resume-only" code path that recovers a dropped stream without
> re-running the LLM, and the per-session event log that backs both.

---

## Problem & motivation

The agent loop emits a stream of events: assistant text deltas, tool
calls, tool results, todo updates, errors. Two natural ways to ship
that over HTTP: WebSockets, or Server-Sent Events. mini_cc chose SSE
for three concrete reasons:

1. **The agent loop is sync and project-locked.** `SessionManager.send()`
   is a regular generator that holds the project lock for the duration
   of the turn. WebSockets would require either rewriting the loop as
   async (huge blast radius) or running the sync loop in a thread
   anyway — at which point SSE gives you the same bridge with a
   simpler client.
2. **One-way traffic is enough.** The client sends one user turn via
   POST, the server streams N events back. There is no second client
   message mid-turn that would justify a bidirectional socket.
3. **HTTP/2 mux + standard `Last-Event-Id` resume**. Browsers and
   proxies already speak SSE; the `EventSource` API auto-reconnects and
   re-sends `Last-Event-Id` for free.

But SSE over a sync iterator introduces four production hazards:
unbounded server buffering when a client is slow, proxy idle-timeout
disconnects at 60s, dropped events when a client disconnects mid-turn,
and the "did the LLM run twice?" ambiguity after reconnect. The
implementation in `mini_cc/server/sse.py` and the B8 fix in
`mini_cc/server/routes/sessions.py` are the answers.

---

## Design & principles

### The sync→async bridge

`sse_stream` (`mini_cc/server/sse.py:36`) takes a sync iterator of
event dicts and yields SSE-formatted chunks. A dedicated
`ThreadPoolExecutor(max_workers=1)` drains the iterator and pushes
items onto a bounded `asyncio.Queue(maxsize=512)`. The async side
awaits `queue.get()` with a heartbeat timeout.

```
   sync_iter (project-locked, in worker thread)
        │  for ev in iter: _dispatch_sync(queue, loop, ("event", ev))
        ▼
   asyncio.Queue(maxsize=512)        ← bounded; slow client → teardown
        │  await asyncio.wait_for(queue.get(), timeout=15s)
        ▼
   yield f"id: {seq}\ndata: {json}\n\n"   ← SSE wire format
```

The bound is the load-bearing decision: a slow client cannot grow the
server's buffer without limit. When `queue.put` would block (queue
full), the worker thread's `run_coroutine_threadsafe(...).result()`
blocks; the async side is busy yielding heartbeats or chunks; the
client disconnect is observed as a `CancelledError`, which triggers
`on_cancel` (which calls `sess.stop()`).

### Wire format & framing

Every event is rendered as (`sse.py:32`):

```
id: 42
data: {"type":"assistant_message","text":"..."}

```

Note the trailing blank line — that's what delimits an SSE event. The
`id:` field is what `EventSource` captures and replays as
`Last-Event-Id` on reconnect. The stream closes with a sentinel
(`sse.py:26`):

```
data: [DONE]

```

so clients can tear down cleanly without a heuristic. Heartbeats
(`sse.py:29`) are SSE comments — a line starting with `:` — which
clients ignore but proxies see as activity:

```
: keepalive

```

### `Last-Event-Id` replay

Every event emitted from `/send` is persisted to a per-session
append-only log *as it streams* (`sessions.py:223`):

```python
for ev in sm.send(pid, sid, body.user_input):
    try:
        sess.loop.project.storage.append_session_event(pid, sid, ev)
    except Exception:
        pass
    yield ev
```

The write is best-effort — a disk failure does not break the live
stream. On reconnect the client passes `Last-Event-Id: 42`; the route
reads everything *after* seq 42 from disk
(`sessions.py:187` calls `storage.read_session_events_since`) and
passes the list as `replay=` to `sse_stream`. The bridge emits the
replay events first, *then* subscribes to the live iterator — so the
client sees a single ordered stream with no gaps and no duplicates.

Sequence continuity is computed in `sse.py:66`:
`next_seq = int(last_event_id) + 1 if last_event_id is not None else 1`.

### B8 resume-only path: don't re-run the LLM

The hard case: client drops at seq 50 of a 100-event turn. The LLM
kept running server-side (the worker thread kept draining the
iterator); events 51-100 landed in the per-session log. When the
client reconnects, you must *not* trigger a new LLM call — that would
double-charge, double-emit, and corrupt the transcript.

`SendMessageRequest.resume` (`schemas.py:31`) is the flag. When true,
the `/send` handler takes a different branch (`sessions.py:196`):

```python
# B8 resume-only mode: skip the lock + dispatch — just replay from
# disk and close. No project lock needed: this is a pure read.
if body.resume:
    def _replay_only_iter():
        for ev in replay:
            yield ev
    return StreamingResponse(
        sse_stream(_replay_only_iter(),
                   last_event_id=last_seq or None,
                   replay=None),
        media_type="text/event-stream",
        headers={..., "X-mini_cc-Resume": "1"},
    )
```

The `X-mini_cc-Resume: 1` response header is how a client confirms
the server understood the request as resume-only (not a fresh dispatch).
If a second `/send` (non-resume) is in flight holding the project lock,
the resume path still works — it never touches the lock.

### Concurrent dispatch guard

When `resume` is *false* and a normal dispatch is requested,
`sm.try_lock(pid)` (`sessions.py:213`) gates the turn: a second send
to the same project while one is in flight returns `409 project_busy`
rather than queuing. SSE does not multiplex turns within a project.

### Client-side reconnect contract

The official client retries with exponential backoff on disconnect:
**1s → 2s → 4s, max 3 attempts**, then surfaces an error to the user.
On each retry it sends `Last-Event-Id: <last seen>` and
`{"user_input": "", "resume": true}`. This pairs with the B8 path:
the server replays the gap, and if the original turn is still running,
the client sees the new live events immediately after the replay.

### Fix history (visible in git log)

- `3b27fb7` HTTP/SSE transport layer (P3) — initial `/send`.
- `d1ad886` `fix(round2/batch4): SSE queue bound + heartbeat + Last-Event-Id replay`
  — added the 512-cap queue, 15s heartbeat, and the replay plumbing.
- `aaa5793` `feat(sse): B8 resume-only path + client auto-reconnect on drop`
  — added the `resume` flag, the resume-only branch, the `X-mini_cc-Resume`
  header, and the client backoff/retry loop.

---

## Operation & configuration

### The `/send` contract

| Element | Value | Source |
|---|---|---|
| Method & path | `POST /tenants/{tid}/projects/{pid}/sessions/{sid}/send` | `sessions.py:157` |
| Auth + scope | `sessions:write` (also consumes a rate-limit token) | `sessions.py:161` |
| Request body | `{"user_input": "...", "resume": false, "model": null}` | `schemas.py:25` |
| Request header | `Last-Event-Id: <int>` (optional, for replay) | `sessions.py:162` |
| Response | `text/event-stream` | `sessions.py:245` |
| Response header | `X-Accel-Buffering: no` (disable nginx buffering) | `sessions.py:253` |
| Response header | `X-mini_cc-Resume: 1` (only when `resume=true`) | `sessions.py:208` |
| Busy response | `409 {"code":"project_busy"}` | `sessions.py:214` |

### SSE tuning constants

| Constant | Default | Meaning | Source |
|---|---|---|---|
| `DEFAULT_MAXSIZE` | `512` | Queue capacity per stream | `sse.py:27` |
| `HEARTBEAT_SECONDS` | `15.0` | Idle interval before a keepalive comment | `sse.py:28` |
| `HEARTBEAT_COMMENT` | `: keepalive\n\n` | The SSE comment frame | `sse.py:29` |
| `_DONE_SENTINEL` | `data: [DONE]\n\n` | End-of-stream marker | `sse.py:26` |

These are not currently exposed as env vars; override by editing
`sse.py` or by passing `maxsize=` / `heartbeat_seconds=` to
`sse_stream` (the function signature already accepts them).

### Proxy / nginx notes

- Disable buffering: the route emits `X-Accel-Buffering: no`, but also
  set `proxy_buffering off` for the `/send` location if your nginx
  config overrides it.
- Idle timeout: bump `proxy_read_timeout` above 60s. The 15s heartbeat
  resets nginx's idle timer, but explicit configuration is safer.
- HTTP/1.1: ensure `proxy_http_version 1.1` — SSE over HTTP/1.0 will
  buffer.

---

## Verification steps

```bash
# 1. Streaming baseline: send a turn, watch id:/data: framing.
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"say hi in one word"}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send
# id: 1
# data: {"type":"..."}
#
# ...
# data: [DONE]
#

# 2. Heartbeat: open a stream that produces slow events; within 15s of
#    idle you'll see ": keepalive" lines that keep the connection alive.

# 3. Replay: note the last id from step 1, then reconnect with it.
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Last-Event-Id: 3' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"more"}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send
# Server emits events from seq 4 onward, then the live stream.

# 4. Resume-only: drop mid-stream, reconnect with resume=true.
curl -N -H "Authorization: Bearer $KEY" \
  -H 'Last-Event-Id: 50' \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"","resume":true}' \
  http://127.0.0.1:8002/tenants/tA/projects/p1/sessions/s1/send \
  | head -c 200
# Response includes header: X-mini_cc-Resume: 1
# Body: only the missed events 51..N, then [DONE]. No new LLM run.

# 5. Concurrency guard: two concurrent sends to the same project.
( curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $K" \
   -d '{"user_input":"long..."}' $URL/send & \
  curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $K" \
   -d '{"user_input":"second"}' $URL/send & wait )
# One returns 200-streaming, the other returns 409 project_busy.

# 6. Cancel propagation: start a stream, kill the client mid-flight.
#    The server's on_cancel → sess.stop() should fire; check server logs
#    for the loop teardown (no hung Anthropic client threads).
```

---

## Common pitfalls / debugging

1. **"My client reconnects but the LLM runs again"**. You forgot
   `resume: true` in the reconnect request body. Without it, the
   server treats the request as a fresh dispatch and may 409 if the
   original turn is still holding the project lock.
2. **"Events appear duplicated after reconnect"**. Your `Last-Event-Id`
   is stale or zero. The server starts replay at `int(last_event_id)+1`;
   if you send `Last-Event-Id: 0` (or omit it) you'll get *everything*
   from seq 1, including events the client already rendered. Always
   persist the highest id you've successfully rendered client-side.
3. **"Stream stalls at exactly 60s"**. A proxy (nginx, Cloudflare,
   ALB) is cutting the idle connection. The 15s heartbeat should
   prevent this, but if the proxy buffers aggressively the comment
   never reaches it. Verify `X-Accel-Buffering: no` is honored, or
   raise the proxy's idle timeout.
4. **"Queue full → silent disconnect"**. When the 512-cap queue fills
   (slow consumer), the bridge tears down the connection. Symptom:
   client sees a clean stream end without `[DONE]`. Diagnose by
   checking server logs for the `CancelledError` path and confirming
   `on_cancel` fired; root cause is always a client that can't drain.
5. **"`resume` works for one session but 404s for another"**. The
   resume path still calls `_ensure_warm` (`sessions.py:172`); if the
   session isn't in memory *and* isn't on disk, you get 404 even
   though the request was resume-only. The per-session event log
   lives on disk under the project — if the project itself was
   deleted, the events are gone.

---

## Further reading

- Source: `mini_cc/server/sse.py`, `mini_cc/server/routes/sessions.py`,
  `mini_cc/server/schemas.py`, project storage's
  `append_session_event` / `read_session_events_since` (in
  `mini_cc/projects/`).
- Git: `aaa5793 feat(sse): B8 resume-only path + client auto-reconnect on drop`,
  `d1ad886 fix(round2/batch4): SSE queue bound + heartbeat + Last-Event-Id replay`.
- Sibling: [07 — HTTP Server & Routing](07-http-server.md),
  [09 — Auth, Scopes, Share Tokens](09-auth.md)
