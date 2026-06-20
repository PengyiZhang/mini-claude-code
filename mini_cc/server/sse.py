"""SSE bridge: sync Iterator[dict] → async iterator of SSE-formatted chunks.

SessionManager.send() is synchronous and project-locked. To stream its
events from an async FastAPI handler we run the sync iterator in a
dedicated thread and forward events through an asyncio.Queue.

Each event dict is rendered as ``data: <json>\\n\\n``. The stream closes
with a ``data: [DONE]\\n\\n`` sentinel so clients can tear down cleanly.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Iterator

_DONE_SENTINEL = "data: [DONE]\n\n"


async def sse_stream(sync_iter: Iterator[dict[str, Any]],
                     on_cancel: "Any | None" = None,
                     ) -> AsyncIterator[str]:
    """Bridge a sync iterator of event dicts to an async SSE stream.

    - Spawns one worker thread (per call) that drains the iterator.
    - Each yielded event is forwarded verbatim as ``data: <json>\\n\\n``.
    - On iterator exhaustion OR exception, emits the ``[DONE]`` sentinel
      and returns.

    `on_cancel` is an optional no-arg callable invoked if the async side
    is cancelled (client disconnect) — the caller can wire this to
    ``session.stop()`` so the next iteration of AgentLoop.run() exits.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    executor = ThreadPoolExecutor(max_workers=1)

    def _worker():
        try:
            for ev in sync_iter:
                payload = f"data: {json.dumps(ev, default=str)}\n\n"
                asyncio.run_coroutine_threadsafe(
                    queue.put(payload), loop).result()
        except Exception as e:
            err = {"type": "error", "message": f"{type(e).__name__}: {e}"}
            payload = f"data: {json.dumps(err)}\n\n"
            try:
                asyncio.run_coroutine_threadsafe(
                    queue.put(payload), loop).result()
            except Exception:
                pass
        finally:
            try:
                asyncio.run_coroutine_threadsafe(
                    queue.put(None), loop).result()
            except Exception:
                pass

    loop.run_in_executor(executor, _worker)

    try:
        while True:
            item = await queue.get()
            if item is None:
                yield _DONE_SENTINEL
                return
            yield item
    except asyncio.CancelledError:
        if on_cancel is not None:
            try:
                on_cancel()
            except Exception:
                pass
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
