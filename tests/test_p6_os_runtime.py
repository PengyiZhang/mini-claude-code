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
