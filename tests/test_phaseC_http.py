"""Phase C: HTTP-driven permission prompt + decision flow."""
from __future__ import annotations

import json
import threading
import time
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.config import set_default_config
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


# ── Mock Anthropic client ──────────────────────────────────────────────────

class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Msg:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _Stream:
    def __init__(self, msg): self._msg = msg
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def __iter__(self): return iter(())
    def get_final_message(self): return self._msg
    def close(self): pass


class _ScriptClient:
    """Replays a script of responses across multiple stream() calls."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

        class _M:
            def stream(s, **kw):
                self.calls.append(kw)
                if not self.script:
                    raise RuntimeError("script exhausted")
                return _Stream(self.script.pop(0))

        self._m = _M()

    @property
    def messages(self): return self._m


class _MockConfig:
    def __init__(self, client): self._client = client
    def build_client(self): return self._client
    def build_provider(self):
        from mini_cc.core.llm import AnthropicProvider
        return AnthropicProvider(self.build_client)
    @property
    def primary_model(self): return "mock-model"
    @property
    def fallback_model(self): return None


@pytest.fixture
def app_with_permissions(tmp_path):
    """App where the project workspace has .mini_cc/permissions.toml."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)

    # Create a project + write permissions.toml into its workspace.
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    ws = tmp_path / "projects" / "p1" / "workspace"
    (ws / ".mini_cc").mkdir(parents=True, exist_ok=True)
    (ws / ".mini_cc" / "permissions.toml").write_text(
        'prompt_tools = ["bash"]\ntimeout_seconds = 30\n')

    yield client, pm, sm, tmp_path

    import mini_cc.config as cfg
    cfg._DEFAULT = None


def _drain_sse_events(resp) -> list[dict]:
    """Parse the SSE lines from a streaming response into event dicts."""
    events: list[dict] = []
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if line.startswith("data: "):
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def test_get_permissions_empty_when_no_pending(app_with_permissions):
    client, *_ = app_with_permissions
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})
    r = client.get("/tenants/tenant1/projects/p1/sessions/s1/permissions",
                   headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_get_permissions_returns_pending_during_prompt(app_with_permissions):
    """When the loop is blocked on a permission_request, GET /permissions
    shows it. POST /decide allow unblocks the loop."""
    client, pm, sm, tmp_path = app_with_permissions

    # Stub client: first turn → bash tool_use; second turn → done.
    script = [
        _Msg([_Block(type="tool_use", name="bash", id="tu1",
                     input={"command": "echo hi > out.txt"})]),
        _Msg([_Block(type="text", text="done")], stop_reason="end_turn"),
    ]
    set_default_config(_MockConfig(_ScriptClient(script)))  # type: ignore[arg-type]

    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})

    # Drive the loop directly via SDK in a worker thread — this
    # sidesteps TestClient's reluctance to stream from a non-main
    # thread. The HTTP routes for /permissions still go through
    # TestClient (main thread).
    sess = sm._ensure_warm("p1", "s1")
    events_collected: list[dict] = []
    request_id_holder: list[str] = []
    send_error: list = []

    def _run_loop():
        try:
            for ev in sess.loop.run("write hi"):
                events_collected.append(ev)
                if (ev.get("type") == "permission_request"
                        and not request_id_holder):
                    request_id_holder.append(ev["request_id"])
        except Exception as e:
            send_error.append(e)

    t = threading.Thread(target=_run_loop)
    t.start()

    # Wait for the permission_request to surface.
    deadline = time.monotonic() + 5
    while not request_id_holder and time.monotonic() < deadline:
        time.sleep(0.05)
    assert request_id_holder, "permission_request never appeared"

    # GET /permissions should now show the pending request.
    r = client.get("/tenants/tenant1/projects/p1/sessions/s1/permissions",
                   headers=AUTH)
    assert r.status_code == 200
    pending = r.json()
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "bash"
    assert pending[0]["request_id"] == request_id_holder[0]

    # POST /decide allow
    req_id = request_id_holder[0]
    r2 = client.post(
        f"/tenants/tenant1/projects/p1/sessions/s1/permissions/{req_id}/decide",
        headers=AUTH, json={"decision": "allow"})
    assert r2.status_code == 204

    t.join(timeout=10)
    assert not t.is_alive()
    if send_error:
        raise send_error[0]

    types = [e["type"] for e in events_collected]
    assert "permission_request" in types
    assert "tool_result" in types
    tr = next(e for e in events_collected if e["type"] == "tool_result")
    assert "[permission" not in tr["content"]


def test_decide_deny_returns_message(app_with_permissions):
    client, pm, sm, tmp_path = app_with_permissions
    script = [
        _Msg([_Block(type="tool_use", name="bash", id="tu1",
                     input={"command": "rm -rf build"})]),
        _Msg([_Block(type="text", text="ok")], stop_reason="end_turn"),
    ]
    set_default_config(_MockConfig(_ScriptClient(script)))  # type: ignore[arg-type]
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s2"})
    sess = sm._ensure_warm("p1", "s2")

    events: list[dict] = []
    req_holder: list[str] = []
    send_error: list = []

    def _run_loop():
        try:
            for ev in sess.loop.run("rm"):
                events.append(ev)
                if (ev.get("type") == "permission_request"
                        and not req_holder):
                    req_holder.append(ev["request_id"])
        except Exception as e:
            send_error.append(e)

    t = threading.Thread(target=_run_loop)
    t.start()
    deadline = time.monotonic() + 5
    while not req_holder and time.monotonic() < deadline:
        time.sleep(0.05)
    assert req_holder, "permission_request never appeared"

    r = client.post(
        f"/tenants/tenant1/projects/p1/sessions/s2/permissions/{req_holder[0]}/decide",
        headers=AUTH, json={"decision": "deny", "message": "nope"})
    assert r.status_code == 204

    t.join(timeout=10)
    assert not t.is_alive()
    if send_error:
        raise send_error[0]
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["content"] == "nope"


def test_decide_unknown_returns_404(app_with_permissions):
    client, *_ = app_with_permissions
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s3"})
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/s3/permissions/nonexistent/decide",
        headers=AUTH, json={"decision": "allow"})
    assert r.status_code == 404


def test_decide_invalid_decision_returns_409(app_with_permissions):
    client, *_ = app_with_permissions
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s4"})
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/s4/permissions/whatever/decide",
        headers=AUTH, json={"decision": "maybe"})
    assert r.status_code == 409


def test_no_config_means_no_interceptor(tmp_path):
    """Project without permissions.toml — GET returns [], POST 404."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)

    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p_noperm"})
    client.post("/tenants/tenant1/projects/p_noperm/sessions",
                headers=AUTH, json={"session_id": "s1"})

    r = client.get(
        "/tenants/tenant1/projects/p_noperm/sessions/s1/permissions",
        headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []

    r2 = client.post(
        "/tenants/tenant1/projects/p_noperm/sessions/s1/permissions/whatever/decide",
        headers=AUTH, json={"decision": "allow"})
    assert r2.status_code == 404
