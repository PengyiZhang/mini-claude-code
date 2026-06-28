"""Round 2 audit Batch 4 tests — SSE queue limit, heartbeat, reconnect
(A5, A6, B8)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from mini_cc.server.sse import (
    DEFAULT_MAXSIZE,
    HEARTBEAT_SECONDS,
    sse_stream,
)
from mini_cc.storage import FSStorage


# ── A5: bounded queue ──────────────────────────────────────────────────────

def test_sse_queue_maxsize_enforced():
    """A small maxsize + finite producer drains correctly. The point is
    that backpressure doesn't deadlock: the async side reads while the
    worker fills, both finish cleanly."""

    async def producer():
        def sync_iter():
            for i in range(20):
                yield {"type": "n", "i": i}

        seen = []
        gen = sse_stream(sync_iter(), maxsize=4, heartbeat_seconds=10.0)
        async for chunk in gen:
            if "[DONE]" in chunk:
                break
            seen.append(chunk)
        return seen

    seen = asyncio.run(asyncio.wait_for(producer(), timeout=5.0))
    # 20 events consumed (no DONE in seen because we break on DONE).
    assert len(seen) == 20
    assert seen[0].startswith("id: 1\n")
    assert seen[-1].startswith("id: 20\n")


# ── A6: heartbeat ──────────────────────────────────────────────────────────

def test_sse_emits_heartbeat_on_idle():
    """When the producer blocks, the consumer must receive keepalive
    comments to defeat idle-proxy timeouts."""

    async def runner():
        def sync_iter():
            yield {"type": "first"}
            time.sleep(0.4)  # long enough to trigger heartbeat at 0.1s
            yield {"type": "second"}

        seen = []
        gen = sse_stream(sync_iter(), heartbeat_seconds=0.1)
        async for chunk in gen:
            seen.append(chunk)
            if len(seen) >= 3:
                break
        return seen

    seen = asyncio.run(asyncio.wait_for(runner(), timeout=5.0))
    assert any(": keepalive" in s for s in seen), seen


# ── B8: event id + replay ──────────────────────────────────────────────────

def test_sse_emits_event_ids_for_last_event_id_resume():
    """Each event must carry an id so Last-Event-Id can pick up where
    it left off."""

    async def runner():
        def sync_iter():
            yield {"type": "a"}
            yield {"type": "b"}

        seen = []
        gen = sse_stream(sync_iter(), heartbeat_seconds=10.0)
        async for chunk in gen:
            seen.append(chunk)
            if "[DONE]" in chunk:
                break
        return seen

    seen = asyncio.run(asyncio.wait_for(runner(), timeout=5.0))
    ids = [c.split("\n")[0] for c in seen if c.startswith("id: ")]
    assert "id: 1" in ids
    assert "id: 2" in ids


def test_sse_replays_buffered_events_before_live():
    """Replay list is emitted first, then live events continue the
    sequence."""

    async def runner():
        def sync_iter():
            yield {"type": "live"}

        replay = [{"type": "past"}, {"type": "older"}]
        seen = []
        gen = sse_stream(sync_iter(), last_event_id=0,
                         replay=replay, heartbeat_seconds=10.0)
        async for chunk in gen:
            seen.append(chunk)
            if "[DONE]" in chunk:
                break
        return seen

    seen = asyncio.run(asyncio.wait_for(runner(), timeout=5.0))
    assert seen[0].startswith("id: 1\n")
    assert "past" in seen[0]
    assert seen[1].startswith("id: 2\n")
    assert "older" in seen[1]
    assert seen[2].startswith("id: 3\n")
    assert "live" in seen[2]


def test_storage_session_event_log_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.append_session_event("p", "s", {"type": "a"})
    s.append_session_event("p", "s", {"type": "b"})
    s.append_session_event("p", "s", {"type": "c"})

    assert s.session_event_count("p", "s") == 3
    # Replay from 0 = all
    all_evts = s.read_session_events_since("p", "s", 0)
    assert [e["type"] for e in all_evts] == ["a", "b", "c"]
    # Replay from 1 = events with seq > 1
    rest = s.read_session_events_since("p", "s", 1)
    assert [e["type"] for e in rest] == ["b", "c"]
    # Replay from 5 = nothing
    assert s.read_session_events_since("p", "s", 5) == []


def test_storage_session_event_log_isolated_per_session(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.append_session_event("p", "s1", {"type": "a"})
    s.append_session_event("p", "s2", {"type": "b"})
    assert s.session_event_count("p", "s1") == 1
    assert s.session_event_count("p", "s2") == 1
    assert s.read_session_events_since("p", "s1", 0)[0]["type"] == "a"


def test_storage_delete_session_removes_event_log(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.append_session_event("p", "s", {"type": "a"})
    fp = tmp_path / "state" / "p" / "sessions" / "s.events.jsonl"
    assert fp.exists()
    s.delete_session("p", "s")
    assert not fp.exists()
