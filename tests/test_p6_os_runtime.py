"""OpenSandboxRuntime: HTTP adapter for the OpenSandbox lifecycle server +
in-sandbox execd agent. All tests monkeypatch urllib.request.urlopen so no
real server is required."""
from __future__ import annotations

import io
import json
import subprocess
from urllib.request import Request

from mini_cc.sandbox.opensandbox_runtime import OpenSandboxConfig


# ── Config ────────────────────────────────────────────────────────────────

def test_config_from_env(monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "sk-test")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "osb.example.com:8080")
    monkeypatch.setenv("OPEN_SANDBOX_PROTOCOL", "https")
    cfg = OpenSandboxConfig.from_env()
    assert cfg.base_url == "https://osb.example.com:8080/v1"
    assert cfg.api_key == "sk-test"
    assert cfg.execd_port == 44772


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_PROTOCOL", raising=False)
    cfg = OpenSandboxConfig.from_env()
    assert cfg.base_url == "http://localhost:8080/v1"
    assert cfg.api_key == ""  # dev mode: server may have no auth


# ── urlopen mock helper ───────────────────────────────────────────────────

class _MockResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _mock_urlopen(monkeypatch, responder):
    """responder: callable(Request) -> (status:int, body:bytes, headers:dict)"""
    def fake(req: Request, *a, **kw):
        status, body, hdrs = responder(req)
        r = _MockResp(body if isinstance(body, bytes) else body.encode())
        r.status = status
        r.headers = {"Content-Type": "application/json", **(hdrs or {})}
        return r
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.urlopen", fake)


# ── is_available ──────────────────────────────────────────────────────────

def test_is_available_true(monkeypatch):
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    _mock_urlopen(monkeypatch, lambda req: (200, b'{"status":"ok"}', {}))
    assert OpenSandboxRuntime(cfg).is_available() is True


def test_is_available_false_on_5xx(monkeypatch):
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    _mock_urlopen(monkeypatch, lambda req: (503, b'{"error":"down"}', {}))
    assert OpenSandboxRuntime(cfg).is_available() is False


def test_is_available_false_on_conn_error(monkeypatch):
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def boom(req, *a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr("mini_cc.sandbox.opensandbox_runtime.urlopen", boom)
    assert OpenSandboxRuntime(cfg).is_available() is False


def test_request_sends_apikey_header(monkeypatch):
    """OPEN-SANDBOX-API-KEY header must be attached when cfg.api_key set."""
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    seen = {}
    def responder(req):
        # urllib title-cases header names; look up case-insensitively
        hdrs = {k.lower(): v for k, v in req.header_items()}
        seen["key"] = hdrs.get("open-sandbox-api-key")
        return (200, b'{"status":"ok"}', {})
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxRuntime(OpenSandboxConfig(
        base_url="http://x/v1", api_key="sk-secret")).is_available()
    assert seen["key"] == "sk-secret"


# ── ensure_running ────────────────────────────────────────────────────────

def test_ensure_running_reuses_existing(monkeypatch):
    """Already a Running sandbox with same tid → reuse, no POST create."""
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k", ready_timeout=5)
    posts = []
    def responder(req: Request):
        if req.method == "GET" and "metadata=" in req.full_url:
            return (200, json.dumps({"items":[
                {"id":"sbx_abc","status":{"state":"Running"},
                 "metadata":{"mini-cc-tid":"t1"}}], "pagination":{}}).encode(), {})
        if req.method == "POST":
            posts.append(req.full_url)
            return (409, b'{"code":"CONFLICT"}', {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxRuntime(cfg).ensure_running(
        name="t1", image="python:3.11",
        mounts=[("/h","/workspaces","")], network="none")
    assert posts == []  # never tried to create


def test_ensure_running_creates_when_missing(monkeypatch):
    """No existing sandbox → POST create; poll Pending → Running."""
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k",
                             ready_timeout=5, poll_interval=0.01)
    state_seq = ["Pending", "Running"]
    created = False
    def responder(req: Request):
        nonlocal created
        if req.method == "GET" and "metadata=" in req.full_url:
            return (200, b'{"items":[],"pagination":{}}', {})
        if req.method == "POST":
            created = True
            return (202, json.dumps(
                {"id":"sbx_new","status":{"state":"Pending"}}).encode(),
                {"Location":"http://x/v1/sandboxes/sbx_new"})
        if req.method == "GET" and req.full_url.endswith("/sandboxes/sbx_new"):
            return (200, json.dumps({"id":"sbx_new",
                    "status":{"state": state_seq.pop(0)}}).encode(), {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    OpenSandboxRuntime(cfg).ensure_running(
        name="t1", image="python:3.11",
        mounts=[("/h","/workspaces","")], network="none")
    assert created
    assert state_seq == []  # polled at least once past Pending


def test_ensure_running_timeout(monkeypatch):
    """ready_timeout exhausted while Pending → RuntimeError."""
    import pytest
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k",
                             ready_timeout=1, poll_interval=0.05)
    def responder(req: Request):
        if req.method == "GET" and "metadata=" in req.full_url:
            return (200, b'{"items":[],"pagination":{}}', {})
        if req.method == "POST":
            return (202, b'{"id":"sbx_x","status":{"state":"Pending"}}', {})
        return (200, b'{"id":"sbx_x","status":{"state":"Pending"}}', {})
    _mock_urlopen(monkeypatch, responder)
    with pytest.raises(RuntimeError, match="never became Running"):
        OpenSandboxRuntime(cfg).ensure_running(
            name="t1", image="python:3.11",
            mounts=[("/h","/workspaces","")], network="none")


def test_ensure_running_create_fails(monkeypatch):
    """POST returns 5xx → RuntimeUnavailable."""
    import pytest
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    from mini_cc.sandbox.runtime import RuntimeUnavailable
    cfg = OpenSandboxConfig(base_url="http://x/v1", api_key="k")
    def responder(req: Request):
        if "metadata=" in req.full_url:
            return (200, b'{"items":[],"pagination":{}}', {})
        if req.method == "POST":
            return (500, b'{"code":"INTERNAL","message":"oops"}', {})
        return (404, b'{}', {})
    _mock_urlopen(monkeypatch, responder)
    with pytest.raises(RuntimeUnavailable):
        OpenSandboxRuntime(cfg).ensure_running(
            name="t1", image="python:3.11",
            mounts=[("/h","/workspaces","")], network="none")
