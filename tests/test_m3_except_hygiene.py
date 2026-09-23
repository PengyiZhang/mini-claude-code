"""M3-3: silent-exception hygiene for the channel inbound worker.

The inbound webhook has already returned 200 by the time the worker
thread runs, so a swallowed exception used to erase the user's message
without a trace. Errors must be logged (warning, with project + session
context) — surfacing to the user is future work, but invisible loss is
a debugging dead end.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from mini_cc.server.routes.channels import _enqueue_inbound_turn


class _Boom(Exception):
    pass


def test_channel_worker_logs_failed_turn(caplog):
    sm = SimpleNamespace(send=_raise_boom)
    project = SimpleNamespace(project_id="p1")
    with caplog.at_level(logging.WARNING, logger="mini_cc"):
        worker = _enqueue_inbound_turn(sm, project, "s1", "hello", {})
        worker.join(timeout=5)

    msgs = [r for r in caplog.records
            if r.name.startswith("mini_cc") and r.levelno >= logging.WARNING]
    assert msgs, "worker swallowed an exception without any warning log"
    joined = " ".join(r.getMessage() for r in msgs)
    assert "p1" in joined and "s1" in joined
    assert any(r.exc_info for r in msgs), "warning must carry the traceback"


def _raise_boom(*a, **k):
    yield {}
    raise _Boom("LLM provider exploded")


# ── M3-5: graceful drain of in-flight channel workers ──────────────

def test_inflight_workers_tracked_and_drained():
    import threading
    from mini_cc.server.routes import channels as chan

    started = threading.Event()
    release = threading.Event()

    def slow_send(pid, sid, text):
        started.set()
        release.wait(5)
        yield {"type": "done"}

    sm = SimpleNamespace(send=slow_send)
    project = SimpleNamespace(project_id="p_drain")
    worker = _enqueue_inbound_turn(sm, project, "s1", "hi", {})
    assert started.wait(5)
    assert worker.is_alive()

    # Drain with a short timeout returns while the worker is still
    # parked on the release event…
    chan.drain_inbound_workers(timeout=0.2)
    assert worker.is_alive()

    release.set()
    # …and a real drain joins it.
    chan.drain_inbound_workers(timeout=5)
    assert not worker.is_alive()
    assert worker not in chan._INBOUND_WORKERS


def test_drain_with_no_workers_is_noop():
    from mini_cc.server.routes import channels as chan
    chan.drain_inbound_workers(timeout=0.1)  # must not raise
