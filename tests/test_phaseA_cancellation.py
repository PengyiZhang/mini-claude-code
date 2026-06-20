"""Phase A: mid-turn Anthropic cancellation.

When ``loop.stop()`` is called while the model stream is in flight,
the loop must exit promptly (no waiting for the SDK to drain the full
response) and emit a final ``done`` event.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage


@dataclass
class _Block:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict | None = None
    id: str | None = None


class _MockResponse:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _SlowStream:
    """Stream that yields events slowly, simulating an in-flight Anthropic call.

    Sets a flag once iteration begins so the test thread knows to issue stop().
    """
    def __init__(self, response, started: threading.Event, tick: threading.Event):
        self._response = response
        self._started = started
        self._tick = tick

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._closed = True
        return False

    def __iter__(self):
        self._started.set()
        # Emit a few "fake" events to give the loop body something to iterate
        # over. Each iteration we sleep briefly and check tick — when the test
        # calls loop.stop(), _stop becomes set and loop.run's `for _ in stream`
        # body will see it on the next iteration.
        for _ in range(20):
            if self._tick.wait(timeout=0.05):
                break
            yield {"type": "ping"}  # loop body just checks _stop; ignores content
        # If we get here without stop, fall through to final
        return

    def get_final_message(self):
        return self._response

    def close(self):
        # Track that close was called — the loop should call this on cancel.
        self._closed = True


class _SlowClient:
    def __init__(self):
        self._started = threading.Event()
        self._cancel_tick = threading.Event()
        self._closed = False
        outer = self

        class _M:
            def stream(s, **kw):
                return _SlowStream(
                    _MockResponse([_Block(type="text", text="done")],
                                  stop_reason="end_turn"),
                    outer._started, outer._cancel_tick)
            def create(s, **kw):
                raise RuntimeError("test should only use stream()")
        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_loop(tmp_path):
    sandbox = SubprocessSandbox("proj-cancel", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    client = _SlowClient()
    ref = ProjectRef(
        project_id="proj-cancel",
        project_root=str(tmp_path / "ws"),
        sandbox=sandbox,
        storage=storage,
        client_factory=lambda: client,
    )
    return AgentLoop(ref, "sess-cancel"), client


def test_stop_mid_stream_aborts_quickly(tmp_path):
    loop, client = _build_loop(tmp_path)

    events: list[dict] = []
    done = threading.Event()

    def run():
        for ev in loop.run("hi"):
            events.append(ev)
        done.set()

    t = threading.Thread(target=run)
    t.start()

    # Wait until the stream is iterating, then signal stop.
    assert client._started.wait(timeout=2), "stream never started"
    loop.stop()
    # Unblock the slow stream so the loop sees _stop on next iteration.
    client._cancel_tick.set()

    assert done.wait(timeout=3), "loop did not exit within 3s after stop"

    t.join(timeout=5)
    assert not t.is_alive(), "run() thread still alive"

    # The loop should have emitted a final done event.
    assert any(e.get("type") == "done" for e in events), \
        f"expected done event, got: {events}"
