"""P0 tests for the HTTP/SSE server: SSE bridge, schemas, errors.

Route-level tests live alongside; they require a running app via
TestClient. The SSE bridge gets its own isolated tests since it's
the trickiest piece.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.server.errors import (BadRequest, Conflict, MiniCCError, NotFound,
                                   Unauthorized, envelope, map_sdk_exception)
from mini_cc.server.schemas import (CreateProjectRequest, CreateSessionRequest,
                                    ProjectOut, SendMessageRequest, SessionOut)
from mini_cc.server.sse import sse_stream


# ── schemas ──────────────────────────────────────────────────────────

def test_create_project_request_defaults():
    req = CreateProjectRequest()
    assert req.project_id is None
    assert req.display_name is None


def test_send_message_request_requires_input():
    with pytest.raises(Exception):
        SendMessageRequest()  # type: ignore[call-arg]


def test_project_out_round_trip():
    p = ProjectOut(project_id="p1", tenant_id="t1",
                   display_name="Disp", created_at="2026-06-20")
    assert p.project_id == "p1"


# ── errors ───────────────────────────────────────────────────────────

def test_envelope_shape():
    e = NotFound("missing", details={"id": "p1"})
    env = envelope(e)
    assert env == {"error": {
        "code": "not_found", "message": "missing", "details": {"id": "p1"}}}


def test_map_keyerror_to_notfound():
    e = map_sdk_exception(KeyError("p1"))
    assert isinstance(e, NotFound)
    assert e.status_code == 404


def test_map_valueerror_already_exists_to_conflict():
    e = map_sdk_exception(ValueError("project_id already exists: x"))
    assert isinstance(e, Conflict)
    assert e.status_code == 409


def test_map_valueerror_generic_to_badrequest():
    e = map_sdk_exception(ValueError("bad input"))
    assert isinstance(e, BadRequest)
    assert e.status_code == 400


def test_map_passthrough_miniccerr():
    orig = Unauthorized("nope")
    assert map_sdk_exception(orig) is orig


def test_map_unknown_exception_to_500():
    e = map_sdk_exception(RuntimeError("oops"))
    assert isinstance(e, MiniCCError)
    assert e.status_code == 500


# ── SSE bridge ───────────────────────────────────────────────────────

def test_sse_stream_yields_events_as_data_lines():
    """A sync iterator of event dicts is bridged to SSE `data:` lines,
    ending with the sentinel `[DONE]`."""
    events = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "bash", "input": {}, "id": "tu1"},
        {"type": "done"},
    ]

    async def fake_sync_iter():
        for ev in events:
            yield ev

    # sse_stream expects a sync iterator (it bridges via a thread).
    # We pass a real sync generator to exercise the threading path.
    def sync_iter():
        yield from events

    async def runner():
        out = []
        async for chunk in sse_stream(sync_iter()):
            out.append(chunk)
        return out

    out = asyncio.run(runner())
    # Each chunk is one `data: <payload>\n\n` unit; the last is the sentinel
    assert out[0] == f'data: {json.dumps(events[0])}\n\n'
    assert out[1] == f'data: {json.dumps(events[1])}\n\n'
    assert out[-1] == "data: [DONE]\n\n"
    # The `done` event itself is also forwarded
    joined = "".join(out)
    assert json.dumps({"type": "done"}) in joined


def test_sse_stream_forwards_error_event():
    def sync_iter():
        yield {"type": "error", "message": "boom"}

    async def runner():
        return [c async for c in sse_stream(sync_iter())]

    out = asyncio.run(runner())
    joined = "".join(out)
    assert '"type": "error"' in joined
    assert "boom" in joined
    # Still ends with the sentinel
    assert out[-1] == "data: [DONE]\n\n"


def test_sse_stream_stops_on_generator_exception():
    def sync_iter():
        yield {"type": "text", "text": "first"}
        raise RuntimeError("upstream blew up")

    async def runner():
        return [c async for c in sse_stream(sync_iter())]

    out = asyncio.run(runner())
    # First event flowed, then the worker died; we should still get the
    # sentinel so the client closes cleanly.
    assert any('"text"' in c for c in out)
    assert out[-1] == "data: [DONE]\n\n"


# ── Route tests ──────────────────────────────────────────────────────

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


class _MockClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        outer = self

        class _Stream:
            def __init__(self_inner, response):
                self_inner._response = response
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
            def __iter__(self_inner):
                return iter(())
            def get_final_message(self_inner):
                return self_inner._response
            def close(self_inner):
                pass

        class _M:
            def create(self_inner, **kw):
                outer.calls.append(kw)
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return outer.script.pop(0)

            def stream(self_inner, **kw):
                outer.calls.append(kw)
                if not outer.script:
                    raise RuntimeError("script exhausted")
                return _Stream(outer.script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


class _MockConfig:
    """Stand-in for AnthropicConfig that returns a scripted client."""
    def __init__(self, client):
        self._client = client

    def build_client(self):
        return self._client

    def build_provider(self):
        # Loop consumes default_config().build_provider(); the mock client
        # is SDK-shaped, so wrap in the real AnthropicProvider.
        from mini_cc.core.llm import AnthropicProvider
        return AnthropicProvider(self.build_client)

    # Used by recovery/retry code paths
    api_key = None
    base_url = None
    primary_model = "claude-sonnet-4-6"
    fallback_model = None


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    """Build a live FastAPI app + TestClient with an in-memory key registry
    pre-seeded with one tenant (tenant1 / mck_testkey)."""
    from mini_cc.config import set_default_config

    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1")
    # Force a known key for test predictability
    reg.revoke(rec.key)
    # Patch the registry file directly with a bare-string value — the
    # registry auto-promotes it to KeyRecord(scopes=["*"], label="migrated")
    # on read.
    import json as _json
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))

    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)

    # Stub the LLM client so sessions don't hit Anthropic
    fake_client = _MockClient([])
    set_default_config(_MockConfig(fake_client))  # type: ignore[arg-type]

    client = TestClient(app)
    yield client, fake_client, reg, pm, sm

    set_default_config.__wrapped__ if hasattr(set_default_config, "__wrapped__") else None
    # Reset config global so other tests aren't affected
    import mini_cc.config as cfg
    cfg._DEFAULT = None


AUTH = {"Authorization": "Bearer mck_testkey"}


def test_health_no_auth_required(app_and_client):
    client, *_ = app_and_client
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── Auth ─────────────────────────────────────────────────────────────

def test_auth_missing_bearer_returns_401(app_and_client):
    client, *_ = app_and_client
    r = client.get("/tenants/tenant1/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_auth_unknown_key_returns_401(app_and_client):
    client, *_ = app_and_client
    r = client.get("/tenants/tenant1/projects",
                   headers={"Authorization": "Bearer mck_bogus"})
    assert r.status_code == 401


def test_auth_tenant_mismatch_returns_403(app_and_client):
    client, *_ = app_and_client
    r = client.get("/tenants/somebody_else/projects",
                   headers=AUTH)
    assert r.status_code == 403


# ── Projects CRUD ────────────────────────────────────────────────────

def test_project_crud_round_trip(app_and_client):
    client, *_ = app_and_client
    # Create
    r = client.post("/tenants/tenant1/projects",
                    headers=AUTH,
                    json={"project_id": "demo", "display_name": "Demo"})
    assert r.status_code == 201
    body = r.json()
    assert body["project_id"] == "demo"
    assert body["tenant_id"] == "tenant1"
    assert body["display_name"] == "Demo"

    # Get
    r = client.get("/tenants/tenant1/projects/demo", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["project_id"] == "demo"

    # List
    r = client.get("/tenants/tenant1/projects", headers=AUTH)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Delete
    r = client.delete("/tenants/tenant1/projects/demo", headers=AUTH)
    assert r.status_code == 204

    # Now GET returns 404
    r = client.get("/tenants/tenant1/projects/demo", headers=AUTH)
    assert r.status_code == 404


def test_project_duplicate_create_returns_409(app_and_client):
    client, *_ = app_and_client
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    r = client.post("/tenants/tenant1/projects",
                    headers=AUTH, json={"project_id": "p1"})
    assert r.status_code == 409


def test_project_id_traversal_rejected(app_and_client):
    client, *_ = app_and_client
    r = client.post("/tenants/tenant1/projects",
                    headers=AUTH,
                    json={"project_id": "../etc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_project_cross_tenant_404(app_and_client):
    """A second tenant cannot read tenant1's project even if they know the id."""
    client, *_ = app_and_client
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "private"})
    # Stand up a second tenant key
    reg = app_and_client[2]
    other_rec = reg.generate("tenant2")
    r = client.get("/tenants/tenant2/projects/private",
                   headers={"Authorization": f"Bearer {other_rec.key}"})
    # project 'private' exists but belongs to tenant1 → 404 from
    # tenant2's perspective (no information leak)
    assert r.status_code == 404


# ── Sessions ─────────────────────────────────────────────────────────

def test_session_lifecycle(app_and_client):
    client, *_ = app_and_client
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    # Start
    r = client.post("/tenants/tenant1/projects/p1/sessions",
                    headers=AUTH, json={"session_id": "s1"})
    assert r.status_code == 201
    body = r.json()
    assert body["project_id"] == "p1"
    assert body["session_id"] == "s1"
    assert body["created"] is True

    # List — now returns SessionMeta objects, not bare session_ids.
    r = client.get("/tenants/tenant1/projects/p1/sessions", headers=AUTH)
    assert r.status_code == 200
    ids = [m["session_id"] for m in r.json()]
    assert "s1" in ids

    # Remove
    r = client.delete("/tenants/tenant1/projects/p1/sessions/s1",
                      headers=AUTH)
    assert r.status_code == 204

    # Remove again → 404
    r = client.delete("/tenants/tenant1/projects/p1/sessions/s1",
                      headers=AUTH)
    assert r.status_code == 404


# ── Send (SSE) ───────────────────────────────────────────────────────

def test_send_streams_sse_events_in_order(app_and_client):
    client, fake_client, *_ = app_and_client
    fake_client.script = [
        _MockResponse([_Block(type="text", text="hello")]),
        _MockResponse([_Block(type="text", text="world")],
                      stop_reason="end_turn"),
    ]
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})

    with client.stream("POST",
                       "/tenants/tenant1/projects/p1/sessions/s1/send",
                       headers=AUTH,
                       json={"user_input": "hi"}) as resp:
        assert resp.status_code == 200
        chunks = []
        for line in resp.iter_lines():
            if line:
                chunks.append(line)

    # Decode data: lines
    payloads = []
    for c in chunks:
        if c.startswith("data: "):
            payload = c[len("data: "):]
            if payload == "[DONE]":
                continue
            payloads.append(json.loads(payload))

    types = [p["type"] for p in payloads]
    assert "text" in types
    assert types[-1] == "done"


def test_send_unknown_session_returns_404(app_and_client):
    client, *_ = app_and_client
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    r = client.post("/tenants/tenant1/projects/p1/sessions/missing/send",
                    headers=AUTH, json={"user_input": "hi"})
    assert r.status_code == 404


def test_send_concurrent_returns_409(app_and_client):
    """While one send holds the project lock, a second send returns 409."""
    client, fake_client, *_ = app_and_client

    # First send blocks until released
    release = threading.Event()
    started = threading.Event()

    class _Blocking:
        def create(self, **kw):
            started.set()
            release.wait(timeout=5)
            return _MockResponse([_Block(type="text", text="done")],
                                 stop_reason="end_turn")

    class _BlockingClient:
        def __init__(self):
            self.calls = []

        @property
        def messages(self):
            class _M:
                def create(s, **kw):
                    if not started.is_set():
                        started.set()
                    release.wait(timeout=5)
                    return _MockResponse(
                        [_Block(type="text", text="done")],
                        stop_reason="end_turn")
                def stream(s, **kw):
                    class _S:
                        def __enter__(self_inner):
                            return self_inner
                        def __exit__(self_inner, *exc):
                            return False
                        def __iter__(self_inner):
                            if not started.is_set():
                                started.set()
                            release.wait(timeout=5)
                            return iter(())
                        def get_final_message(self_inner):
                            return _MockResponse(
                                [_Block(type="text", text="done")],
                                stop_reason="end_turn")
                        def close(self_inner):
                            pass
                    return _S()
            return _M()

    # Reset the fake client to the blocking one
    fake_client.__class__ = _BlockingClient

    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s2"})

    # Kick off send on s1 in a thread, then probe s2 send from main
    result = {}

    def first_send():
        with client.stream("POST",
                           "/tenants/tenant1/projects/p1/sessions/s1/send",
                           headers=AUTH,
                           json={"user_input": "x"}) as resp:
            for _ in resp.iter_lines():
                pass
        result["first_status"] = resp.status_code

    t = threading.Thread(target=first_send)
    t.start()
    started.wait(timeout=3)

    # Second concurrent send to same project → 409
    r = client.post("/tenants/tenant1/projects/p1/sessions/s2/send",
                    headers=AUTH, json={"user_input": "y"})
    assert r.status_code == 409
    assert r.json()["error"]["details"]["code"] == "project_busy"

    release.set()
    t.join(timeout=5)

