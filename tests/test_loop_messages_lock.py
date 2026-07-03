"""Phase I.B-2.1: serialize nudge writes to loop.messages via a
pending-nudges queue drained by _run_impl.

Pre-fix race: AgentLoop.nudge(content) did
``self.messages.append(...)`` from the watcher daemon thread while
``_run_impl`` was concurrently mutating ``self.messages`` on the
request thread. The load-bearing failure was a lost write:

    1. ``_run_impl`` calls ``compact_history(self.messages, ...)`` —
       this returns a brand-new compacted list, snapshotting the
       messages at call time.
    2. While that call is in flight (or in the gap before the
       slice assignment runs), the watcher daemon calls ``nudge``,
       which appends to ``self.messages``.
    3. ``_run_impl`` then does
       ``self.messages[:] = compacted`` — a slice assignment that
       overwrites the live list with the compacted snapshot. The
       nudge content is silently dropped.

Post-fix (Option B: queue + drain): ``nudge`` appends to a separate
``_pending_nudges`` list (single-threaded with the watcher), and
``_run_impl`` drains that list at the top of every iteration via
``_inject_pending_nudges``. The only writer of ``loop.messages``
from the nudge path is ``_run_impl`` itself, on its own thread —
no race surface.
"""
from __future__ import annotations

import time
from threading import Thread

import pytest

# Reuse test scaffolding from the existing lead mailbox test.
from test_lead_mailbox_inject import (
    _Block, _MockResponse, _MockClient, _SpawnerStub, _build_loop,
)


def _wait_for(predicate, timeout=10.0, interval=0.02):
    """Spin until predicate() is truthy or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_nudge_during_compact_is_not_lost(tmp_path):
    """The load-bearing race regression.

    Synthesize the race directly: monkey-patch ``compact_history`` so
    the moment it is called from inside ``_run_impl``, the watcher
    daemon fires a nudge, AND monkey-patch
    ``is_prompt_too_long_error`` to force the prompt-too-long /
    compact branch on the first stream call.

    With the direct-append implementation (pre-fix), the nudge
    content lands in ``self.messages`` between the
    ``compact_history(...)`` snapshot being taken and the slice
    assignment ``self.messages[:] = ...`` overwriting the list — so
    the nudge is silently dropped.

    With the queue+drain fix, the nudge is appended to
    ``_pending_nudges`` (never to ``self.messages``) and is drained
    on the next iteration via ``_inject_pending_nudges``."""
    script = [
        # First response: consumed on the second iteration (after the
        # compact branch triggered by the forced prompt-too-long on
        # the first iteration).
        _MockResponse([_Block(type="text", text="after compact")]),
    ]
    loop, _ = _build_loop(tmp_path, script)

    # Force the prompt-too-long branch on the first stream attempt so
    # the compact_history slice-assignment path runs. We do this by
    # wrapping the provider's stream method to raise on the first
    # call. Then patch compact_history to fire a nudge mid-call (the
    # exact race window: between snapshot taken and slice assign
    # committed).
    import mini_cc.core.loop as loop_mod

    real_compact = loop_mod.compact_history
    real_is_ptl = loop_mod.is_prompt_too_long_error
    nudge_fired = {"yes": False}
    compact_calls = {"n": 0}

    class _ForcedPTL(Exception):
        status_code = 400

    def spying_compact(messages, *args, **kwargs):
        compact_calls["n"] += 1
        # Take the snapshot that compact_history would return — a
        # separate list, so the slice assignment in _run_impl will
        # overwrite the live self.messages with this snapshot's
        # contents (whatever was on the list at snapshot time).
        snapshot = list(messages)
        # Simulate the watcher firing AFTER compact_history has
        # computed its result but BEFORE _run_impl commits
        # ``self.messages[:] = snapshot``. With the pre-fix direct-
        # append nudge, the appended content is on ``self.messages``
        # already — and the slice assignment in _run_impl will
        # overwrite it. This is the exact race window the fix closes.
        if not nudge_fired["yes"]:
            nudge_fired["yes"] = True
            loop.nudge("[mid-compact milestone]")
        return snapshot

    ptl_calls = {"n": 0}

    def force_ptl(e):
        ptl_calls["n"] += 1
        # Only the first stream call (which raised _ForcedPTL) is
        # treated as prompt-too-long; subsequent calls fall through
        # to the real check so a normal end_turn returns cleanly.
        if ptl_calls["n"] == 1:
            return True
        return real_is_ptl(e)

    real_stream = loop.provider.stream

    def raising_then_real(**kw):
        if ptl_calls["n"] == 0:
            raise _ForcedPTL("simulated prompt too long")
        return real_stream(**kw)

    # Replace the provider's stream method on the bound instance so
    # _run_impl's _open_stream picks up the wrapper.
    loop.provider.stream = raising_then_real  # type: ignore
    loop_mod.compact_history = spying_compact
    loop_mod.is_prompt_too_long_error = force_ptl
    try:
        events = []
        for ev in loop.run("hello"):
            events.append(ev)
    finally:
        loop_mod.compact_history = real_compact
        loop_mod.is_prompt_too_long_error = real_is_ptl

    # Sanity: the compact branch did run (otherwise the test isn't
    # exercising the race at all).
    assert compact_calls["n"] >= 1, (
        "spying_compact was never called — race window not exercised; "
        f"events: {events}"
    )
    # The nudge content must eventually land in loop.messages.
    found = [
        m for m in loop.messages
        if isinstance(m.get("content"), str)
        and "mid-compact milestone" in m["content"]
    ]
    assert found, (
        "nudge fired during compact was lost — slice assignment "
        f"overwrote it. messages: {loop.messages}"
    )


def test_nudge_during_active_send_appears_in_next_turn(tmp_path):
    """While /send is running a multi-iteration turn, a nudge from
    the watcher daemon must end up in loop.messages by the time the
    run completes."""
    # First response carries a tool_use so the loop iterates again;
    # the second response ends the turn cleanly.
    script = [
        _MockResponse(
            [_Block(type="tool_use", name="bash", id="t1",
                    input={"command": "sleep 0.1"})],
            stop_reason="tool_use",
        ),
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, _ = _build_loop(tmp_path, script)

    def _run():
        list(loop.run("go"))

    t = Thread(target=_run)
    t.start()

    # Wait until the loop is running.
    assert _wait_for(lambda: getattr(loop, "_running", False)), \
        "loop didn't start"
    # Fire a nudge mid-flight from a separate (daemon-like) thread.
    loop.nudge("[concurrent milestone]")

    t.join(timeout=60)
    assert not t.is_alive(), "loop thread did not finish"

    found = [
        m for m in loop.messages
        if isinstance(m.get("content"), str)
        and "concurrent milestone" in m["content"]
    ]
    assert found, (
        "nudge content was lost when fired during an active /send; "
        f"messages: {loop.messages}"
    )


def test_multiple_nudges_during_running_loop_all_land(tmp_path):
    """Three nudges fired while the loop is running must ALL land in
    loop.messages, in the order they were queued."""
    script = [
        _MockResponse(
            [_Block(type="tool_use", name="bash", id="t1",
                    input={"command": "sleep 0.1"})],
            stop_reason="tool_use",
        ),
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, _ = _build_loop(tmp_path, script)

    def _run():
        list(loop.run("go"))

    t = Thread(target=_run)
    t.start()
    assert _wait_for(lambda: getattr(loop, "_running", False)), \
        "loop didn't start"

    loop.nudge("[nudge A]")
    loop.nudge("[nudge B]")
    loop.nudge("[nudge C]")

    t.join(timeout=60)
    assert not t.is_alive()

    contents = [m.get("content") for m in loop.messages
                if isinstance(m.get("content"), str)]
    a = next((i for i, c in enumerate(contents) if c and "nudge A" in c), None)
    b = next((i for i, c in enumerate(contents) if c and "nudge B" in c), None)
    c = next((i for i, c in enumerate(contents) if c and "nudge C" in c), None)
    assert (a, b, c) != (None, None, None), (
        f"missing nudges in messages: {loop.messages}"
    )
    assert a < b < c, (
        f"nudges landed out of order: A={a} B={b} C={c}; "
        f"messages: {loop.messages}"
    )


def test_nudge_from_watcher_daemon_thread_is_safe(tmp_path):
    """Calling nudge from a separate thread while _run_impl is
    iterating must not raise or corrupt the message list. This is
    the bare thread-safety property the watcher daemon relies on."""
    script = [
        _MockResponse(
            [_Block(type="tool_use", name="bash", id="t1",
                    input={"command": "sleep 0.1"})],
            stop_reason="tool_use",
        ),
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, _ = _build_loop(tmp_path, script)

    errors: list = []

    def _run():
        try:
            list(loop.run("go"))
        except Exception as e:
            errors.append(e)

    t = Thread(target=_run)
    t.start()
    assert _wait_for(lambda: getattr(loop, "_running", False)), \
        "loop didn't start"

    # Hammer nudge from another thread.
    nd = Thread(
        target=lambda: [loop.nudge(f"[hammer {i}]") for i in range(5)],
        name="fake-watcher",
    )
    nd.start()
    nd.join(timeout=30)
    assert not nd.is_alive(), "watcher thread blocked in nudge"

    t.join(timeout=60)
    assert not t.is_alive()
    assert errors == [], f"loop raised: {errors}"

    # All hammer nudges landed.
    contents = " ".join(
        m.get("content", "") for m in loop.messages
        if isinstance(m.get("content"), str)
    )
    for i in range(5):
        assert f"[hammer {i}]" in contents, (
            f"hammer nudge {i} lost; messages: {loop.messages}"
        )


def test_pending_nudges_drained_in_run_until_idle_path(tmp_path):
    """The daemon-spawned path (_run_until_idle → _run_impl(None))
    must also drain _pending_nudges. This guards the case where
    nudge fires while the loop is idle and the daemon worker is the
    only thing draining the queue."""
    script = [
        _MockResponse([_Block(type="text", text="first")]),
        _MockResponse([_Block(type="text", text="drained")]),
    ]
    loop, _ = _build_loop(tmp_path, script)
    list(loop.run("seed"))  # consume first response

    # Loop is idle now. Nudge from the watcher thread.
    loop.nudge("[daemon milestone]")
    # Wait for the spawned _run_until_idle to finish.
    assert _wait_for(
        lambda: not getattr(loop, "_running", True)
        and any(isinstance(m.get("content"), str)
                and "daemon milestone" in m["content"]
                for m in loop.messages),
        timeout=10,
    ), (
        f"daemon-path nudge was not drained; messages: {loop.messages}"
    )
