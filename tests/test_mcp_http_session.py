"""MCP Streamable HTTP session-ID negotiation.

Per MCP spec, the server assigns a session id on the ``initialize``
response (returned in the ``Mcp-Session-Id`` header) and requires the
client to echo it back on every subsequent request. Servers that
enforce this — e.g. the user's ``web-mcp`` endpoint at
``172.28.76.218:22222/mcp`` — reject requests without the header with
``HTTP 400 Bad Request: Missing session ID``.

Pre-fix the client never captured or sent the header, so any
session-enforcing server failed at ``tools/list`` immediately after
``initialize`` succeeded. The fix captures the header from any
response that carries it and includes it on every subsequent
request.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from mini_cc.mcp.http import HttpMCPClient, HttpMCPError


class _FakeResponse:
    """Stand-in for the urlopen context manager. Carries headers so the
    client can pluck ``Mcp-Session-Id`` out of the initialize response."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._buf = io.BytesIO(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1):
        return self._buf.read() if n < 0 else self._buf.read(n)


def _make_http_error(code: int, reason: str, body: bytes):
    import urllib.error
    return urllib.error.HTTPError(
        url="http://x/mcp",
        code=code,
        msg=reason,
        hdrs=None,
        fp=io.BytesIO(body),
    )


# ── Capture + echo ────────────────────────────────────────────────────


def test_initialize_captures_session_id_and_echoes_on_subsequent():
    """The defining behavior: server's ``Mcp-Session-Id`` response
    header on ``initialize`` MUST be echoed back on ``tools/list``.
    Pre-fix this is the exact failure the user hit on web-mcp:
    "HTTP 400 Bad Request — Missing session ID"."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        # First request = initialize; assign session id.
        if len(captured) == 1:
            body = json.dumps({
                "jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "fake", "version": "0"},
                    "capabilities": {},
                }}).encode()
            return _FakeResponse(body, {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "sess-abc-123",
            })
        # Subsequent requests must carry the session id we just captured.
        if "Mcp-session-id" not in req.headers:
            raise _make_http_error(400, "Bad Request",
                b'{"error":"Missing session ID"}')
        body = json.dumps({
            "jsonrpc": "2.0", "id": len(captured),
            "result": {"tools": [
                {"name": "ping", "description": "p",
                 "inputSchema": {"type": "object"}}
            ]}}).encode()
        return _FakeResponse(body, {"Content-Type": "application/json"})

    with patch("mini_cc.mcp.http.urllib.request.urlopen",
               side_effect=fake_urlopen):
        client = HttpMCPClient("srv", "http://x.invalid/mcp")
        client.startup()  # initialize + tools/list

    # initialize: no session id yet (nothing to echo).
    assert "Mcp-session-id" not in captured[0]
    # tools/list: session id MUST be present and match what the
    # server assigned.
    assert captured[1].get("Mcp-session-id") == "sess-abc-123", (
        "After initialize returns Mcp-Session-Id, every subsequent "
        "request must echo it back — servers enforce this.")
    # And the client ended up connected with the discovered tool.
    assert len(client.tools) == 1
    assert client.tools[0]["name"] == "ping"


def test_client_works_when_server_does_not_assign_session_id():
    """Backwards compat: older MCP servers (and the spec's
    session-optional mode) never return ``Mcp-Session-Id``. The client
    must not invent one or break — it just proceeds header-less."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        body = json.dumps({
            "jsonrpc": "2.0", "id": len(captured), "result": {}}).encode()
        return _FakeResponse(body, {"Content-Type": "application/json"})

    with patch("mini_cc.mcp.http.urllib.request.urlopen",
               side_effect=fake_urlopen):
        client = HttpMCPClient("srv", "http://x.invalid/mcp")
        client.startup()

    # Neither request should carry a session id.
    for hdrs in captured:
        assert "Mcp-session-id" not in hdrs
    assert client._session_id is None


def test_session_id_persists_across_calls():
    """Once captured, the session id stays on the client and is sent
    on every request thereafter (tools/list, tools/call, ...)."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        rid = len(captured)
        if rid == 1:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
            return _FakeResponse(body, {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "persistent-sid",
            })
        if rid == 2:
            body = json.dumps({"jsonrpc": "2.0", "id": 2,
                "result": {"tools": []}}).encode()
        else:
            body = json.dumps({"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": "ok"}]}}).encode()
        return _FakeResponse(body, {"Content-Type": "application/json"})

    with patch("mini_cc.mcp.http.urllib.request.urlopen",
               side_effect=fake_urlopen):
        client = HttpMCPClient("srv", "http://x.invalid/mcp")
        client.startup()
        client.call_tool("ping", {})

    # startup() sends initialize + notifications/initialized + tools/list,
    # then call_tool adds one more. We just verify the echo pattern:
    # request 0 (initialize) has no sid; everything after does.
    assert len(captured) >= 3
    assert "Mcp-session-id" not in captured[0]
    for i, hdrs in enumerate(captured[1:], start=1):
        assert hdrs.get("Mcp-session-id") == "persistent-sid", (
            f"request #{i} missing session id echo")


def test_session_id_refreshed_when_server_rotates_it():
    """Some servers rotate the session id periodically (or on
    reauth). The client should pick up the latest value from any
    response that carries the header, not stick with the original.

    Setup: initialize assigns 'old-sid'; the next response (notify
    ack) carries 'rotated-sid'. After that point, every outgoing
    request must use 'rotated-sid' — using 'old-sid' would fail
    server-side validation."""
    captured: list[dict] = []
    state = {"rotation_done": False}

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        rid = len(captured)
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}).encode()
        if rid == 1:
            return _FakeResponse(body, {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "old-sid",
            })
        if rid == 2:
            # Rotate here.
            state["rotation_done"] = True
            return _FakeResponse(body, {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "rotated-sid",
            })
        return _FakeResponse(body, {"Content-Type": "application/json"})

    with patch("mini_cc.mcp.http.urllib.request.urlopen",
               side_effect=fake_urlopen):
        client = HttpMCPClient("srv", "http://x.invalid/mcp")
        client.startup()
        client.call_tool("ping", {})

    # Request 1 (initialize): no sid yet (we haven't seen one).
    assert "Mcp-session-id" not in captured[0]
    # Request 2 (notify): echoes old-sid (the only one we've seen).
    assert captured[1].get("Mcp-session-id") == "old-sid"
    # Requests 3+ : rotation response has been processed, all use new sid.
    for i, hdrs in enumerate(captured[2:], start=2):
        assert hdrs.get("Mcp-session-id") == "rotated-sid", (
            f"request #{i+1} should use rotated sid; got {hdrs}")


def test_user_supplied_headers_are_preserved_with_session_id():
    """Custom Authorization headers etc. must still go out alongside
    the auto-added session id."""
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        rid = len(captured)
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}).encode()
        if rid == 1:
            return _FakeResponse(body, {
                "Content-Type": "application/json",
                "Mcp-Session-Id": "with-auth",
            })
        return _FakeResponse(body, {"Content-Type": "application/json"})

    with patch("mini_cc.mcp.http.urllib.request.urlopen",
               side_effect=fake_urlopen):
        client = HttpMCPClient("srv", "http://x.invalid/mcp",
                               headers={"Authorization": "Bearer tok"})
        client.startup()

    # Authorization persisted on every request.
    assert captured[0].get("Authorization") == "Bearer tok"
    assert captured[1].get("Authorization") == "Bearer tok"
    # Session id present on second request.
    assert captured[1].get("Mcp-session-id") == "with-auth"
