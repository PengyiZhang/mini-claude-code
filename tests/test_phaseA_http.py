"""Phase A: end-to-end rate-limit and trace-id middleware behavior."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.ratelimit import TenantRateLimiter
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def rate_app(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    import json as _json
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    limiter = TenantRateLimiter(default_rpm=2)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    rate_limiter=limiter)
    yield TestClient(app)


def test_send_429_when_rate_limit_exceeded(rate_app):
    client = rate_app
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    client.post("/tenants/tenant1/projects/p1/sessions",
                headers=AUTH, json={"session_id": "s1"})

    # Bucket capacity=2. create_project burned 1. First send consumes the
    # last token (returns 200); second send within window gets 429.
    r1 = client.post("/tenants/tenant1/projects/p1/sessions/s1/send",
                     headers=AUTH, json={"user_input": "hi"})
    assert r1.status_code == 200
    r2 = client.post("/tenants/tenant1/projects/p1/sessions/s1/send",
                     headers=AUTH, json={"user_input": "again"})
    assert r2.status_code == 429
    body = r2.json()
    assert body["error"]["details"]["code"] == "rate_limited"
    assert "Retry-After" in r2.headers


def test_trace_id_returned_in_response_header(rate_app):
    """Every response gets an X-Trace-Id; inbound one is preserved."""
    client = rate_app
    # Inbound trace id is echoed back
    r = client.get("/health", headers={"X-Trace-Id": "abc"})
    assert r.headers.get("X-Trace-Id") == "abc"
    # Absent inbound → server mints one
    r = client.get("/health")
    tid = r.headers.get("X-Trace-Id")
    assert tid
    assert len(tid) == 32  # uuid4 hex
