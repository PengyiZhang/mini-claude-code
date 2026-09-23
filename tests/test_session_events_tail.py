"""debug.10: ``GET /sessions/{sid}/events`` long-lived tail stream + real
event-log seq on the wire.

The bug this suite guards against: daemon-driven lead turns (LeadWatcher
nudges via scheduled ``schedule_wakeup`` firings or teammate replies)
were persisted to ``events.jsonl`` but never reached the main chat UI
live, because the request-scoped ``/send`` SSE stream had already
closed. The fix is the dual of ``/send``:

1. A long-lived SSE endpoint that tails the per-session event log.
2. Both ``/send`` and ``/events`` stamp the REAL event-log seq into the
   SSE ``id:`` line, so the frontend dedups the same record delivered by
   both channels (skip if seq ≤ max applied).

Coverage in this file:
- ``read_session_events_since_with_seq`` returns ``(seq, payload)`` tuples.
- ``sse_stream`` honors a pre-assigned seq from ``(seq, payload)`` tuples
  yielded by the iterator / passed in ``replay``.
- ``GET /events`` replays missed events when ``Last-Event-Id`` is sent.
- ``GET /events`` skips history on a fresh connect (no ``Last-Event-Id``)
  — callers hydrate from ``/messages`` first.
- ``/send`` stamps the real event-log seq into ``id:`` so the client can
  dedup against the tail stream.
"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.sse import _format_event
from mini_cc.session import SessionManager
from mini_cc.storage.fs import FSStorage


# ── Storage layer ────────────────────────────────────────────────────


def test_read_session_events_since_with_seq_returns_tuples(tmp_path):
    """``read_session_events_since_with_seq`` returns ``(seq, payload)``
    tuples so ``sse_stream`` can forward the real seq to the SSE
    ``id:`` line. The legacy ``read_session_events_since`` (payloads
    only) stays seq-unaware for backward compat."""
    s = FSStorage(tmp_path)
    seq1 = s.append_session_event("p", "s", {"type": "text", "text": "a"})
    seq2 = s.append_session_event("p", "s", {"type": "text", "text": "b"})
    seq3 = s.append_session_event("p", "s", {"type": "done"})

    out = s.read_session_events_since_with_seq("p", "s", 0)
    assert out == [
        (seq1, {"type": "text", "text": "a"}),
        (seq2, {"type": "text", "text": "b"}),
        (seq3, {"type": "done"}),
    ], f"expected (seq, payload) tuples; got {out}"

    # since=seq2 returns only the third.
    rest = s.read_session_events_since_with_seq("p", "s", seq2)
    assert rest == [(seq3, {"type": "done"})]


# ── sse_stream tuple handling ────────────────────────────────────────


def test_sse_stream_passes_pre_assigned_seq(monkeypatch):
    """``sse_stream`` must forward a tuple's pre-assigned seq as the SSE
    ``id:`` instead of auto-incrementing. This is what lets the
    frontend's per-session dedup (maxAppliedSeq) collapse the same
    record arriving from both ``/send`` and the ``/events`` tail."""
    import asyncio
    from mini_cc.server.sse import sse_stream

    async def _run():
        # Iterator yields (seq, payload) tuples.
        def _iter():
            yield (101, {"type": "text", "text": "first"})
            yield (102, {"type": "done"})
        chunks = []
        async for chunk in sse_stream(_iter()):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    # The first chunk must carry id: 101 (not id: 1).
    assert "id: 101" in chunks[0], (
        f"expected pre-assigned seq 101 in SSE id line; got: {chunks[0]}"
    )
    assert "id: 102" in chunks[1], (
        f"expected pre-assigned seq 102 in SSE id line; got: {chunks[1]}"
    )
    # Sentinel still goes out at the end.
    assert chunks[-1].startswith("data: [DONE]")


def test_sse_stream_replay_uses_pre_assigned_seq():
    """Replay entries (caller-supplied) also honor the pre-assigned seq.
    ``/events`` passes ``(seq, payload)`` tuples in ``replay`` so the
    replayed batch lands with the same seqs the log assigned them."""
    import asyncio
    from mini_cc.server.sse import sse_stream

    async def _run():
        def _empty():
            return
            yield  # make it a generator
        replay = [
            (5, {"type": "text", "text": "replayed-a"}),
            (6, {"type": "text", "text": "replayed-b"}),
        ]
        out = []
        async for chunk in sse_stream(_empty(), replay=replay):
            out.append(chunk)
        return out

    out = asyncio.run(_run())
    assert "id: 5" in out[0]
    assert "id: 6" in out[1]


def test_format_event_uses_provided_seq():
    """``_format_event`` writes the seq as the SSE id: line verbatim —
    the wire contract every dedup-capable caller relies on."""
    chunk = _format_event({"type": "text", "text": "x"}, 42)
    assert chunk.startswith("id: 42\n"), f"unexpected chunk: {chunk!r}"


# ── HTTP: GET /sessions/{sid}/events ─────────────────────────────────


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def app(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    return TestClient(build_app(
        data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
    ))


def _setup(client, pid="p1"):
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": pid})
    client.post(f"/tenants/tenant1/projects/{pid}/sessions",
                headers=AUTH, json={"session_id": "s1"})


def test_events_endpoint_replays_missed_events_with_last_event_id(app,
                                                                  monkeypatch):
    """A reconnecting client sends ``Last-Event-Id`` and gets back every
    event with seq > last since the log began. Replayed events carry
    their REAL log seq on the ``id:`` line (not a stream-local
    counter) so the client dedups correctly.

    Starlette's ``TestClient`` buffers streaming response bodies, so we
    can't drain a 10-minute-long tail through it. We shrink
    ``_TAIL_POLL_INTERVAL``/``_TAIL_IDLE_TIMEOUT`` and patch ``time.sleep``
    so the tailer exits on idle within ~0.1s, the response closes
    naturally, and TestClient returns the buffered replay batch.
    """
    _setup(app)
    storage = app.app.state.pm.get("p1", tenant_id="tenant1").storage
    s1 = storage.append_session_event("p1", "s1", {"type": "text", "text": "a"})
    s2 = storage.append_session_event("p1", "s1", {"type": "text", "text": "b"})
    s3 = storage.append_session_event("p1", "s1", {"type": "done"})

    from mini_cc.server.routes import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "_TAIL_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sessions_mod, "_TAIL_IDLE_TIMEOUT", 0.05)

    r = app.get(
        "/tenants/tenant1/projects/p1/sessions/s1/events",
        headers={**AUTH, "Last-Event-Id": str(s1)},
    )
    assert r.status_code == 200, r.text
    seqs: list[int] = []
    pending = 0
    for raw in r.text.splitlines():
        line = raw.strip()
        if line.startswith("id:"):
            try:
                pending = int(line[3:].strip())
            except ValueError:
                pass
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                seqs.append(pending)
    assert s2 in seqs and s3 in seqs, (
        f"expected replay of seq {s2} and {s3}; got seqs {seqs} "
        f"(body: {r.text!r})"
    )
    assert s1 not in seqs, (
        f"replay should skip seq {s1} (it's the Last-Event-Id); got {seqs}"
    )


def test_events_endpoint_skips_history_on_fresh_connect(app, monkeypatch):
    """A fresh connect (no ``Last-Event-Id``) must NOT replay existing
    log entries. The caller hydrates from ``/messages`` first; the tail
    only delivers events written AFTER connect. Without this guard, every
    page mount would re-render the entire transcript through the tail
    stream and double every bubble.

    Same short-timeout trick as the replay test so TestClient can drain.
    """
    _setup(app)
    storage = app.app.state.pm.get("p1", tenant_id="tenant1").storage
    MARKER = "PRECONNECT_MARKER_2b71"
    storage.append_session_event(
        "p1", "s1", {"type": "text", "text": MARKER})
    storage.append_session_event("p1", "s1", {"type": "done"})

    from mini_cc.server.routes import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "_TAIL_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sessions_mod, "_TAIL_IDLE_TIMEOUT", 0.05)

    # Fresh connect (no Last-Event-Id).
    r = app.get(
        "/tenants/tenant1/projects/p1/sessions/s1/events",
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert MARKER not in r.text, (
        f"fresh /events connect replayed pre-existing log entries — "
        f"the page-mount chat would double every bubble. Body: {r.text!r}"
    )


def test_events_endpoint_returns_404_for_unknown_session(app):
    """Unknown session → 404, not a 500 from the tail generator crashing
    on a missing log file."""
    _setup(app)
    r = app.get(
        "/tenants/tenant1/projects/p1/sessions/nope/events",
        headers=AUTH,
    )
    assert r.status_code == 404, r.text


# ── HTTP: /send stamps real event-log seq ────────────────────────────


def test_send_sse_id_matches_event_log_seq(app):
    """debug.10: ``/send``'s SSE ``id:`` line must match the real event-
    log seq (not a stream-local counter that resets to 1 on every
    fresh POST). Without alignment the frontend's per-session dedup
    (``maxAppliedSeq``) can't collapse records delivered by both
    ``/send`` and ``/events``.

    Setup: seed one event so the log's seq is non-zero, then issue a
    fresh /send and assert the first event's id: line is the next seq
    in the log — NOT 1.
    """
    _setup(app)
    storage = app.app.state.pm.get("p1", tenant_id="tenant1").storage
    # Pre-seed so the log's seq counter isn't 0 at the next /send.
    pre_seq = storage.append_session_event(
        "p1", "s1", {"type": "text", "text": "seeded-pre-send"})

    # Fresh /send (no Last-Event-Id).
    r = app.post(
        "/tenants/tenant1/projects/p1/sessions/s1/send",
        headers=AUTH,
        json={"user_input": "hello"},
    )
    assert r.status_code == 200, r.text
    # Find the first id: line that pairs with a data: line.
    first_seq = None
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            try:
                first_seq = int(line[3:].strip())
            except ValueError:
                continue
            break
    assert first_seq is not None, (
        f"no id: line in /send response; body was: {r.text!r}"
    )
    # The first event must be seq > pre_seq (the next log slot), not 1.
    assert first_seq > pre_seq, (
        f"/send stamped id: {first_seq} — looks stream-local (should be "
        f"> {pre_seq}, the pre-seeded seq). Body: {r.text!r}"
    )
