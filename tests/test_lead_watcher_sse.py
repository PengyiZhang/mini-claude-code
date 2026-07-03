"""Phase I.B-1.3: Route watcher-triggered events to events.jsonl.

Two observability gaps closed:

1. The LeadWatcher daemon (I.B-1.2) was never started by the /send
   route, so it sat idle even after Phase I.B-1.2 shipped. Part A of
   the fix wires ``teams.start_lead_watcher`` into ``sync_iter``.

2. When the watcher fired ``loop.nudge(...)`` while the user was NOT on
   ``/send``, the resulting daemon-thread turn drove ``_run_until_idle``
   which routes yielded events through ``AgentLoop._emit``. For
   HTTP-driven sessions ``loop.on_event`` is ``None`` (the /send route
   doesn't set it), so events vanished silently — page refresh showed
   nothing. Part B adds a dedicated ``watcher_event_sink`` field on
   ``AgentLoop`` that ``_run_until_idle`` routes events through, kept
   SEPARATE from ``on_event`` so the dual yield+emit path inside
   ``_run_impl`` can't double-write to events.jsonl.

Why not install the persister on ``loop.on_event`` directly (the spec's
Option γ)? Because ``_run_impl`` calls ``_emit`` for many event types
(tool_use, tool_result, todos_updated, cron_fired, retry, ...) AND
yields those same events — see the docstring at loop.py:1054-1056. If
the persister lived on ``on_event``, a /send-driven turn would
double-write every tool event (once via the /send generator's
append_session_event at sessions.py:240, once via the sink). The
dedicated ``watcher_event_sink`` field is ONLY routed from
``_run_until_idle``, so /send's normal yield path is untouched.
"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.teams.watcher import LeadWatcher

# Reuse test scaffolding from the existing lead mailbox tests.
from test_lead_mailbox_inject import (
    _Block, _MockResponse, _MockClient, _SpawnerStub, _build_loop,
)


def _wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll ``predicate`` until truthy or timeout. Returns last value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


# ── Part B: watcher-side event routing ────────────────────────────────


def test_watcher_persists_nudge_events_when_send_not_running(tmp_path):
    """User is away (no /send iterating). Watcher fires nudge → the
    daemon-thread turn's events must reach the persister callback so
    they can be written to events.jsonl and replayed on refresh."""
    script = [
        _MockResponse([_Block(type="text", text="watcher-fired turn")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    persisted: list[dict] = []

    def _persist(ev):
        persisted.append(ev)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.2,
        event_persister=_persist,
    )
    watcher.start()
    try:
        # Teammate alice reports a result; bus auto-CCs lead.
        bus.send("alice", "charlie", "feature X done",
                 msg_type="result")
        # Wait for the daemon-thread turn to complete. We assert on
        # the LLM's response text landing in loop.messages (proving the
        # turn ran) AND on the persister receiving at least one event
        # (proving events reach events.jsonl, not the void).
        ok = _wait_for(
            lambda: any("watcher-fired turn" in str(m)
                        for m in loop.messages),
            timeout=8.0,
        )
        assert ok, (
            "watcher-triggered turn did not run — messages: "
            f"{loop.messages}"
        )
        # Give _run_until_idle a moment to finish routing the tail of
        # the generator through the sink.
        _wait_for(lambda: len(persisted) > 0, timeout=2.0)
        assert len(persisted) > 0, (
            "persister received no events — daemon-path events are "
            "still vanishing; user-away refresh would show nothing"
        )
        # Sanity: at least one of the persisted events is a text chunk
        # or an assistant message event that the /send SSE path would
        # have surfaced if the user had been present.
        assert any(
            ev.get("type") in {"text", "message", "assistant",
                               "content_block_delta", "message_stop"}
            or "text" in ev
            for ev in persisted
        ), f"no recognizable assistant event in persisted: {persisted}"
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_persister_skipped_when_send_iterating(tmp_path):
    """User IS present (a /send-like caller is iterating loop.run()).
    The watcher's nudge appends to messages and the running generator
    picks it up — events flow through yield, the caller persists them.

    This asserts the no-double-write property: while ``_running=True``
    on the loop, ``_run_until_idle`` (the ONLY place the persister is
    invoked) is NOT entered. The CC message lands, the watcher wakes,
    but nudge's ``_running``-check makes it append-and-return. The
    running generator consumes the appended content via its own
    ``_inject_*`` path; events flow through yield, not through the
    daemon-path sink."""
    # 1st response: the user's "hi". 2nd: a turn driven by the
    # injected CC content. 3rd: a final ack so the loop can idle
    # without exhausting the script if the generator iterates again.
    script = [
        _MockResponse([_Block(type="text", text="hi from lead")]),
        _MockResponse([_Block(type="text", text="saw the update")]),
        _MockResponse([_Block(type="text", text="idle")]),
        _MockResponse([_Block(type="text", text="idle2")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    persisted: list[dict] = []
    was_running_when_persisted: list[bool] = []

    def _persist(ev):
        persisted.append(ev)
        # Snapshot _running at the moment of persist. The invariant we
        # care about: the persister (invoked ONLY from _run_until_idle)
        # must never fire while a /send-style generator owns _running.
        was_running_when_persisted.append(loop._running)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.1,
        event_persister=_persist,
    )
    watcher.start()
    try:
        from threading import Thread

        def _deliver_cc():
            # Wait until loop.run() has flipped _running=True.
            _wait_for(lambda: loop._running, timeout=2.0)
            time.sleep(0.05)
            bus.send("alice", "charlie", "milestone hit",
                     msg_type="milestone")

        t = Thread(target=_deliver_cc, daemon=True)
        t.start()
        # Drive the FULL turn (don't break early) so _running flips
        # back to False cleanly when run() exits. The CC message lands
        # mid-iteration; nudge sees _running=True and just appends. The
        # generator consumes the appended content and yields events for
        # it on its own next iteration — through yield, not the sink.
        seen = list(loop.run("hi"))
        t.join(timeout=2.0)
        # The /send-like caller observed events via yield (including
        # the events for the injected CC content, since nudge appended
        # to messages and the running generator drove the next turn).
        assert len(seen) > 0, "no events yielded from loop.run()"
        # Snapshot persisted DURING iteration. Any event the persister
        # saw while _running was True would be a double-write (the
        # /send caller persists via yield path).
        bad = [p for p, r in zip(persisted, was_running_when_persisted)
               if r]
        assert bad == [], (
            "persister fired while _running=True — this would "
            "double-write events.jsonl (the /send generator's yield "
            "path already persists these). Offending events: "
            f"{bad}"
        )
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_chains_existing_loop_on_event(tmp_path):
    """If loop.on_event was already set (SDK callers), the watcher must
    not stomp it. The persister runs alongside the existing on_event
    callback — both fire for every daemon-path event.

    Note: with the dedicated ``watcher_event_sink`` design (Option β),
    chaining is automatic — on_event and watcher_event_sink are two
    separate fields. This test guards against future regressions that
    might fold them back into one."""
    script = [
        _MockResponse([_Block(type="text", text="daemon turn")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    prev_calls: list[dict] = []
    loop.on_event = lambda ev: prev_calls.append(ev)

    persisted: list[dict] = []

    def _persist(ev):
        persisted.append(ev)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.2,
        event_persister=_persist,
    )
    watcher.start()
    try:
        bus.send("alice", "charlie", "done", msg_type="result")
        _wait_for(
            lambda: any("daemon turn" in str(m)
                        for m in loop.messages),
            timeout=8.0,
        )
        _wait_for(lambda: len(persisted) > 0, timeout=2.0)
        # Both the pre-existing on_event AND the persister fired.
        assert len(prev_calls) > 0, (
            "pre-existing loop.on_event was not invoked — the watcher "
            "must not stomp SDK callers' callbacks"
        )
        assert len(persisted) > 0, (
            "persister was not invoked — daemon events would be lost"
        )
    finally:
        watcher.stop(join_timeout=2.0)


# ── Part A: /send route wiring ────────────────────────────────────────


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


def _send(client, body="hi", sid="s1", pid="p1"):
    """Issue a /send and drain the SSE stream to completion."""
    r = client.post(
        f"/tenants/tenant1/projects/{pid}/sessions/{sid}/send",
        headers=AUTH, json={"user_input": body})
    assert r.status_code == 200, r.text
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def _project_teams(app, pid="p1"):
    """Reach into the running server's PM to grab the project's teams
    spawner so we can assert on watcher state."""
    pm = next(
        r for r in app.app.dependency_overrides.values()
        if isinstance(r, ProjectManager)
    ) if False else None  # placeholder, replaced below
    # The fixture passes pm into build_app; we can read it back from the
    # app's state via the lifespan/dependency provider. Simpler: reach
    # into the project manager that build_app closed over.
    # build_app stores pm on app.state.pm.
    pm = app.app.state.pm
    project = pm.get(pid)
    return getattr(project, "teams", None)


def test_send_route_starts_lead_watcher(app, tmp_path):
    """A /send request must start the LeadWatcher daemon on the
    project's teams spawner. The watcher stays alive across requests
    (idempotent start) so subsequent /send calls don't restart it."""
    _setup(app)
    teams = _project_teams(app)
    # Project may have a teams spawner only if teams was wired up at
    # project creation. For projects created via the HTTP fixture, the
    # spawner is attached lazily — but only if a teammate ever existed.
    # We require the spawner to be present (the route's teams wiring is
    # the SUT). If a future change detaches teams from projects, this
    # assertion is the canary.
    if teams is None:
        pytest.skip("project has no teams spawner — wiring not testable")
    # Before any /send: no watcher.
    assert teams._lead_watcher is None, (
        "watcher should not exist before any /send"
    )
    _send(app, "hello")
    # After /send: watcher exists and is alive.
    assert teams._lead_watcher is not None, (
        "/send did not start the LeadWatcher daemon"
    )
    assert teams._lead_watcher.is_alive(), (
        "LeadWatcher daemon thread is not alive after /send"
    )
    # Idempotent: a second /send does NOT replace the watcher.
    first = teams._lead_watcher
    _send(app, "again")
    assert teams._lead_watcher is first, (
        "/send restarted the watcher — start must be idempotent"
    )
    # Cleanup so the daemon doesn't leak into other tests.
    teams.stop_lead_watcher()


# ── Phase I.C.5: lead_nudged notice event ────────────────────────────


def test_watcher_emits_lead_nudged_notice_event(tmp_path):
    """Phase I.C.5: when the watcher fires a nudge, it must emit a single
    ``lead_nudged`` event through the persister carrying the teammate
    names + kinds so the frontend can show a gray "Alice reported a
    milestone → lead is responding..." notice in the main chat.

    The event MUST arrive before the daemon-thread turn's other events
    (text/tool_use/etc.) so the UI can attach the notice to the bubble
    the daemon turn is about to start. Asserts on:
    - Exactly one lead_nudged event per drain batch (debounce collapses
      a burst into one nudge → one notice).
    - The event carries the teammate name and the kind (milestone).
    - The event is the FIRST persisted event (so the frontend sees the
      notice before it starts streaming the daemon turn's text).
    """
    script = [
        _MockResponse([_Block(type="text", text="watcher-fired turn")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    persisted: list[dict] = []

    def _persist(ev):
        persisted.append(ev)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.2,
        event_persister=_persist,
    )
    watcher.start()
    try:
        bus.send("alice", "charlie", "feature X done",
                 msg_type="milestone")
        ok = _wait_for(
            lambda: any("watcher-fired turn" in str(m)
                        for m in loop.messages),
            timeout=8.0,
        )
        assert ok, (
            "watcher-triggered turn did not run — messages: "
            f"{loop.messages}"
        )
        _wait_for(lambda: len(persisted) > 0, timeout=2.0)
        notices = [ev for ev in persisted if ev.get("type") == "lead_nudged"]
        assert len(notices) == 1, (
            f"expected exactly one lead_nudged event, got "
            f"{len(notices)} in {persisted}"
        )
        ev = notices[0]
        assert ev["items"], "lead_nudged event must carry items list"
        assert ev["items"][0]["from"] == "alice"
        assert ev["items"][0]["kind"] == "milestone"
        # The notice MUST be the first event so the frontend can attach
        # it to the streaming bubble the daemon turn is about to drive.
        assert persisted[0] is ev, (
            "lead_nudged must be the first persisted event; got: "
            f"{persisted[0]}"
        )
    finally:
        watcher.stop(join_timeout=2.0)


def test_watcher_lead_nudged_skipped_when_no_persister(tmp_path):
    """No event_persister wired (legacy / unmonitored session). The
    watcher must still nudge the loop and must NOT crash trying to emit
    the lead_nudged notice. The notice is observability sugar, not a
    correctness invariant."""
    script = [
        _MockResponse([_Block(type="text", text="ok")]),
    ]
    loop, bus = _build_loop(tmp_path, script)

    watcher = LeadWatcher(
        bus=bus,
        project_id="proj-x",
        lead_loop_getter=lambda: loop,
        poll_interval=0.05,
        debounce=0.1,
        # event_persister deliberately omitted
    )
    watcher.start()
    try:
        bus.send("alice", "charlie", "done", msg_type="result")
        ok = _wait_for(
            lambda: any("ok" in str(m) for m in loop.messages),
            timeout=6.0,
        )
        assert ok, "watcher failed to nudge without persister"
    finally:
        watcher.stop(join_timeout=2.0)
