"""Phase I.C.1 — backend aggregation endpoint for unified team activity.

``GET /tenants/{tid}/projects/{pid}/team/activity`` merges events.jsonl
records across every session in the project, filters by ts (and
optionally by teammate), and returns a single sorted timeline. The UI
polls this every 4s (Task I.C.3) to drive TeammatesPanel.

Design notes captured in docs/plans/2026-07-03-phaseI-team-orchestration-design.md
(Task I.C.1, lines 341-356):
  - ts is stamped onto the JSONL record at append time (Option 1 in the
    plan), so the reader doesn't have to synthesise it from payload
    shape (which varies — text, card, todos_updated, etc.).
  - read_session_events_since semantics stay seq-based for SSE resume.
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


def _setup_project(client, pid="p1"):
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": pid})


def _append_event(client, pid, sid, event, *, sleep=0.0):
    """Append a raw event to the session's events.jsonl via the SDK
    storage API. Sleeps briefly between appends when called repeatedly
    so each record gets a distinct ts (millisecond-precision ISO stamps
    can collide on fast machines)."""
    if sleep:
        time.sleep(sleep)
    project = client.app.state.pm.get(pid, tenant_id="tenant1")
    project.storage.append_session_event(pid, sid, event)


def _activity(client, pid="p1", **params):
    r = client.get(
        f"/tenants/tenant1/projects/{pid}/team/activity",
        headers=AUTH, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ── Tests ─────────────────────────────────────────────────────────────────


def test_activity_returns_empty_for_project_with_no_sessions(app):
    """Fresh project + zero sessions: empty timeline, has_more False."""
    _setup_project(app)
    body = _activity(app)
    assert body == {"events": [], "has_more": False}


def test_activity_returns_events_across_sessions_sorted_by_ts(app):
    """Two sessions, each contributing events. The merged timeline must
    be ascending by ts regardless of which session produced it."""
    _setup_project(app)
    _append_event(app, "p1", "sess-A",
                  {"type": "text", "text": "A1"}, sleep=0.005)
    _append_event(app, "p1", "sess-B",
                  {"type": "text", "text": "B1"}, sleep=0.005)
    _append_event(app, "p1", "sess-A",
                  {"type": "text", "text": "A2"}, sleep=0.005)

    body = _activity(app)
    texts = [e.get("text") for e in body["events"]]
    assert texts == ["A1", "B1", "A2"], texts
    # ts is present on every record and ascending.
    tss = [e["ts"] for e in body["events"]]
    assert tss == sorted(tss)
    # session_id is attached to each event.
    assert {e["session_id"] for e in body["events"]} == {"sess-A", "sess-B"}


def test_activity_filters_by_since(app):
    """?since=<iso> returns only events with ts strictly greater than
    the supplied timestamp."""
    _setup_project(app)
    _append_event(app, "p1", "sess-A",
                  {"type": "text", "text": "old"}, sleep=0.01)
    body_mid = _activity(app)
    mid_ts = body_mid["events"][0]["ts"]

    _append_event(app, "p1", "sess-A",
                  {"type": "text", "text": "new1"}, sleep=0.01)
    _append_event(app, "p1", "sess-A",
                  {"type": "text", "text": "new2"}, sleep=0.01)

    body = _activity(app, since=mid_ts)
    texts = [e["text"] for e in body["events"]]
    assert texts == ["new1", "new2"], texts


def test_activity_filters_by_teammate(app):
    """?teammate=alice restricts the timeline to events from the
    teammate-alice session only. Lead events (no teammate- prefix) are
    excluded."""
    _setup_project(app)
    # Lead session
    _append_event(app, "p1", "sess-lead",
                  {"type": "text", "text": "lead"}, sleep=0.005)
    # Teammate alice
    _append_event(app, "p1", "teammate-alice",
                  {"type": "text", "text": "alice1"}, sleep=0.005)
    # Teammate bob
    _append_event(app, "p1", "teammate-bob",
                  {"type": "text", "text": "bob1"}, sleep=0.005)

    body = _activity(app, teammate="alice")
    texts = [e["text"] for e in body["events"]]
    assert texts == ["alice1"], texts
    sids = {e["session_id"] for e in body["events"]}
    assert sids == {"teammate-alice"}, sids


def test_activity_respects_limit_and_sets_has_more(app):
    """?limit=N truncates the timeline to N events. If more were
    available, has_more must be True."""
    _setup_project(app)
    for i in range(5):
        _append_event(app, "p1", "sess-A",
                      {"type": "text", "text": f"e{i}"}, sleep=0.005)

    body = _activity(app, limit=2)
    assert len(body["events"]) == 2
    assert body["has_more"] is True
    # The earliest two events come back first (ascending ts).
    texts = [e["text"] for e in body["events"]]
    assert texts == ["e0", "e1"], texts

    # limit >= count → has_more False
    body_all = _activity(app, limit=200)
    assert body_all["has_more"] is False


def test_activity_requires_tenant_scope(app):
    """No Authorization header → 401. The endpoint must NOT be public."""
    _setup_project(app)
    r = app.get("/tenants/tenant1/projects/p1/team/activity")
    assert r.status_code == 401, r.text
