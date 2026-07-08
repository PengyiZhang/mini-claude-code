"""MCP stdio transport — newline-delimited JSON (NDJSON) framing.

Modern MCP servers (Playwright MCP v0.0.77+, anything tracking the
2025-06-18 spec revision) speak **newline-delimited JSON** over stdio:
each JSON-RPC message is a single line terminated by ``\n``. The older
LSP-style ``Content-Length: <n>\r\n\r\n{json}`` framing is a legacy
from early MCP drafts inherited from the Language Server Protocol and
no longer matches what real-world servers emit.

Pre-fix the client only parsed ``Content-Length`` headers. A server
that emits NDJSON (no header bytes at all) made the reader block
forever on ``readline()`` waiting for a ``\r\n`` terminator that never
arrived — surfaced to users as "timeout calling `initialize`" after
``STARTUP_TIMEOUT_S`` seconds.

These tests pin the NDJSON contract: outgoing messages are
``{json}\n``, incoming messages are parsed line-by-line.
"""
from __future__ import annotations

import io
import json
import threading
import time

from mini_cc.mcp.stdio import StdioMCPClient


class _FakeReader:
    """readline() drains a shared bytearray that a writer thread fills."""

    def __init__(self, proc):
        self._proc = proc

    def readline(self):
        while True:
            with self._proc._lock:
                buf = self._proc._out_buf
                idx = buf.find(b"\n")
                if idx >= 0:
                    line = bytes(buf[:idx + 1])
                    del buf[:idx + 1]
                    return line
            if self._proc.terminated and not buf:
                return b""
            time.sleep(0.005)

    def read(self, n):
        while True:
            with self._proc._lock:
                buf = self._proc._out_buf
                if len(buf) >= n:
                    chunk = bytes(buf[:n])
                    del buf[:n]
                    return chunk
            if self._proc.terminated and not buf:
                return b""
            time.sleep(0.005)


class _FakeProc:
    """Scripts stdin→stdout: emits the next queued reply each time the
    client writes a *request* (JSON-RPC message with an ``id``).
    Notifications (no ``id``) don't advance the reply pointer — that
    mirrors real servers, which only respond to requests, and avoids a
    race where the buffered reply is dropped because the waiter hasn't
    been registered yet."""

    def __init__(self, replies: list[bytes]):
        self._replies = list(replies)
        self._lock = threading.Lock()
        self.stdin = io.BytesIO()
        self._out_buf = bytearray()
        self.terminated = False
        self.stdout = _FakeReader(self)
        self.stderr = io.BytesIO()
        self._writer = threading.Thread(target=self._loop, daemon=True)
        self._writer.start()

    def _loop(self):
        seen = 0
        last_pos = 0
        while not self.terminated:
            data = self.stdin.getvalue()
            if len(data) > last_pos:
                # Slice the new bytes off and check if they form a
                # complete NDJSON request (has id, ends with \n).
                new_bytes = data[last_pos:]
                last_pos = len(data)
                # Find the last \n-terminated chunk.
                while b"\n" in new_bytes:
                    line, _, new_bytes = new_bytes.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    # Only advance for requests (with id); skip
                    # notifications like notifications/initialized.
                    if "id" not in msg:
                        continue
                    if seen < len(self._replies):
                        with self._lock:
                            self._out_buf.extend(self._replies[seen])
                        seen += 1
            time.sleep(0.005)

    def terminate(self): self.terminated = True
    def kill(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def poll(self): return None


def _ndjson(payload: dict) -> bytes:
    """Frame a JSON-RPC message as one newline-terminated JSON line."""
    return json.dumps(payload).encode("utf-8") + b"\n"


# ── Reader: NDJSON input → parsed messages ────────────────────────────


def test_reader_parses_ndjson_line():
    """The defining behavior: a single ``{json}\n`` line on stdout must
    be parsed as one JSON-RPC message. Pre-fix the reader sat in
    ``_read_content_length`` waiting for ``Content-Length:`` headers
    that NDJSON servers never send."""
    init = _ndjson({
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
    })
    tools = _ndjson({
        "jsonrpc": "2.0", "id": 2,
        "result": {"tools": [
            {"name": "ping", "description": "p",
             "inputSchema": {"type": "object"}},
        ]},
    })
    proc = _FakeProc([init, tools])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()
    try:
        assert len(client.tools) == 1
        assert client.tools[0]["name"] == "ping"
    finally:
        client.close()


def test_startup_succeeds_with_ndjson_only_server():
    """End-to-end: NDJSON server (no Content-Length anywhere) completes
    the full initialize + tools/list handshake. This is the exact
    failure users hit on Playwright MCP v0.0.77."""
    init = _ndjson({"jsonrpc": "2.0", "id": 1, "result": {}})
    tools = _ndjson({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    proc = _FakeProc([init, tools])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()  # must not raise
    assert client._initialized is True
    client.close()


def test_call_tool_over_ndjson():
    """After startup, tools/call also round-trips through NDJSON."""
    init = _ndjson({"jsonrpc": "2.0", "id": 1, "result": {}})
    tools = _ndjson({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
        {"name": "echo", "inputSchema": {"type": "object"}}]}})
    call = _ndjson({"jsonrpc": "2.0", "id": 3, "result": {
        "content": [{"type": "text", "text": "hi"}]}})
    proc = _FakeProc([init, tools, call])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()
    assert client.call_tool("echo", {}) == "hi"
    client.close()


def test_blank_lines_between_messages_are_ignored():
    """Some servers emit keep-alive blank lines between messages; the
    reader must skip them rather than crash on empty JSON. The noise
    is bundled with the next real reply so the fixture emits both
    when the matching request arrives."""
    init = _ndjson({"jsonrpc": "2.0", "id": 1, "result": {}})
    tools_with_noise = b"\n" + _ndjson({
        "jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    proc = _FakeProc([init, tools_with_noise])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()
    client.close()


def test_outgoing_messages_use_ndjson_framing():
    """The wire format we WRITE must match too: a JSON object followed
    by a single ``\n``, no ``Content-Length`` header. A server
    expecting NDJSON would choke on LSP-framed input."""
    init = _ndjson({"jsonrpc": "2.0", "id": 1, "result": {}})
    tools = _ndjson({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    proc = _FakeProc([init, tools])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()
    # Capture stdin BEFORE close() closes the BytesIO.
    written = proc.stdin.getvalue()
    client.close()
    # No Content-Length header anywhere in outgoing bytes.
    assert b"Content-Length" not in written
    # Each outgoing line is JSON-parseable and \n-terminated.
    lines = [ln for ln in written.split(b"\n") if ln]
    assert len(lines) >= 2  # initialize + tools/list at minimum
    for ln in lines:
        msg = json.loads(ln)
        assert msg["jsonrpc"] == "2.0"
        assert "method" in msg


def test_malformed_line_does_not_kill_reader():
    """A garbage line (bad UTF-8, non-JSON) must be skipped, not abort
    the reader thread — same tolerant-reader principle as before. The
    garbage is bundled with the next real reply."""
    init = _ndjson({"jsonrpc": "2.0", "id": 1, "result": {}})
    garbage_then_tools = (b"not-json-at-all\n" + _ndjson({
        "jsonrpc": "2.0", "id": 2, "result": {"tools": []}}))
    proc = _FakeProc([init, garbage_then_tools])
    client = StdioMCPClient("svc", ["fake"], proc=proc)
    client.startup()  # garbage skipped, tools/list still arrives
    client.close()
