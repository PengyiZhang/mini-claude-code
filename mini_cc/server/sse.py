"""SSE bridge: sync Iterator[dict] → async iterator of SSE-formatted chunks.

SessionManager.send() is synchronous and project-locked. To stream its
events from an async FastAPI handler we run the sync iterator in a
dedicated thread and forward events through an asyncio.Queue.

Each event dict is rendered as ``id: <seq>\\ndata: <json>\\n\\n`` so
clients can resume via ``Last-Event-Id``. The stream closes with a
``data: [DONE]\\n\\n`` sentinel so clients can tear down cleanly.

Production hardening:
- ``asyncio.Queue(maxsize=DEFAULT_MAXSIZE)`` — a slow client can't grow
  the server's buffer without bound. When full we close the connection
  (treat it as a disconnect); the worker thread is cancelled.
- Heartbeat: if no event flows within ``HEARTBEAT_SECONDS``, we emit
  ``: keepalive\\n\\n`` so proxies (nginx, cloudflare) don't kill the
  connection at their default 60s idle timeout.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Iterator, Optional

_DONE_SENTINEL = "data: [DONE]\n\n"
DEFAULT_MAXSIZE = 512
HEARTBEAT_SECONDS = 15.0
HEARTBEAT_COMMENT = ": keepalive\n\n"


def _format_event(payload: dict[str, Any], seq: int) -> str:
    return f"id: {seq}\ndata: {json.dumps(payload, default=str)}\n\n"


async def sse_stream(sync_iter: Iterator[dict[str, Any]],
                     on_cancel: "Any | None" = None,
                     *,
                     maxsize: int = DEFAULT_MAXSIZE,
                     heartbeat_seconds: float = HEARTBEAT_SECONDS,
                     last_event_id: Optional[int] = None,
                     replay: Optional[list[dict[str, Any]]] = None,
                     ) -> AsyncIterator[str]:
    """Bridge a sync iterator of event dicts to an async SSE stream.

    - Spawns one worker thread (per call) that drains the iterator.
    - Each yielded event is forwarded as ``id: <seq>\\ndata: <json>\\n\\n``.
    - On iterator exhaustion OR exception, emits the ``[DONE]`` sentinel
      and returns.
    - Emits ``: keepalive\\n\\n`` every ``heartbeat_seconds`` of idle.
    - If the queue fills (slow consumer), the connection is torn down
      and ``on_cancel`` is invoked.

    Parameters
    ----------
    last_event_id:
        Client-supplied ``Last-Event-Id`` header. Recorded for sequence
        continuity — replay is caller-driven.
    replay:
        Optional list of events to emit before subscribing to the live
        iterator. Caller-supplied (e.g. from a per-session event log).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
    executor = ThreadPoolExecutor(max_workers=1)
    next_seq = int(last_event_id) + 1 if last_event_id is not None else 1

    if replay:
        for ev in replay:
            yield _format_event(ev, next_seq)
            next_seq += 1

    def _worker():
        try:
            for ev in sync_iter:
                _dispatch_sync(queue, loop, ("event", ev))
        except Exception as e:
            err = {"type": "error",
                   "message": f"{type(e).__name__}: {e}"}
            _dispatch_sync(queue, loop, ("event", err))
        finally:
            _dispatch_sync(queue, loop, ("done", None))

    def _dispatch_sync(queue, loop, item):
        """Schedule a put on the loop and block until it lands.

        Closes the coroutine explicitly if scheduling fails (loop closed
        mid-flight) so we don't leak ``coroutine never awaited`` warnings
        during teardown.
        """
        coro = queue.put(item)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.result()
        except Exception:
            coro.close()

    loop.run_in_executor(executor, _worker)

    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield HEARTBEAT_COMMENT
                continue
            kind, payload = item
            if kind == "done":
                yield _DONE_SENTINEL
                return
            yield _format_event(payload, next_seq)
            next_seq += 1
    except asyncio.CancelledError:
        if on_cancel is not None:
            try:
                on_cancel()
            except Exception:
                pass
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
