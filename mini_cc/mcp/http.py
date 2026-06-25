"""MCP consumer transport — Streamable HTTP (and legacy SSE).

Two remote transports live here:

- ``HttpMCPClient``  — MCP's Streamable HTTP transport (RFC: a single
  endpoint that accepts POST JSON-RPC and returns either a single JSON
  response or ``text/event-stream`` of JSON-RPC messages). We accept
  both, but only block for the *first* result/hydrate the tools list.
- ``SseMCPClient``   — older transport: GET ``<url>`` opens an event
  stream; the server emits an ``endpoint`` event whose data is the URL
  we POST JSON-RPC to. Still common in the wild, so supported.

Both honour ``headers`` (typical use: ``Authorization: Bearer …``).

Designed to mirror :class:`StdioMCPClient` so :class:`MCPPool` treats
them uniformly.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from .client import MCPClient
from .stdio import _extract_text


class HttpMCPError(RuntimeError):
    """Raised when the remote MCP endpoint is unreachable or misbehaves."""


class _RemoteMCPClientBase(MCPClient):
    """Shared scaffolding for HTTP/SSE clients.

    Both transports boil down to: send a JSON-RPC request, get a JSON-RPC
    response back, demux by id. Subclasses implement ``_post_request``.
    """

    STARTUP_TIMEOUT_S = 15.0
    CALL_TIMEOUT_S = 60.0

    def __init__(self, name: str, url: str,
                 *, headers: dict[str, str] | None = None):
        super().__init__(name)
        if not isinstance(url, str) or not url:
            raise HttpMCPError(
                f"MCP server `{name}`: url must be a non-empty string")
        self.url = url
        self.headers: dict[str, str] = {"Accept": "application/json, text/event-stream",
                                        "Content-Type": "application/json"}
        if headers:
            self.headers.update({str(k): str(v) for k, v in headers.items()})
        self._next_id = 1
        self._lock = threading.Lock()
        self._initialized = False

    # Subclass hook: return the raw JSON-RPC response dict.
    def _post_request(self, payload: dict, *, timeout: float) -> dict:
        raise NotImplementedError

    def _request(self, method: str, params: dict, *, timeout: float) -> Any:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id,
                   "method": method, "params": params}
        try:
            response = self._post_request(payload, timeout=timeout)
        except HttpMCPError:
            raise
        except Exception as e:
            raise HttpMCPError(
                f"MCP server `{self.name}`: `{method}` failed: {e}") from e
        if not isinstance(response, dict):
            raise HttpMCPError(
                f"MCP server `{self.name}`: `{method}` returned non-object")
        if response.get("error"):
            err = response["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HttpMCPError(
                f"MCP server `{self.name}`: `{method}` error: {msg}")
        return response.get("result")

    def startup(self) -> None:
        if self._initialized:
            return
        init_result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini_cc", "version": "0.1.0"},
        }, timeout=self.STARTUP_TIMEOUT_S)
        _ = init_result  # server capabilities; we don't currently branch on them
        try:
            self._post_notify("notifications/initialized", {})
        except Exception:
            pass
        tools_result = self._request("tools/list", {},
                                     timeout=self.STARTUP_TIMEOUT_S)
        raw_tools = (tools_result or {}).get("tools", []) if isinstance(
            tools_result, dict) else []
        self.tools = []
        for t in raw_tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            self.tools.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema")
                                or {"type": "object", "properties": {}},
            })
        self._handlers = {}
        self._initialized = True

    def _post_notify(self, method: str, params: dict) -> None:
        # Default: same as request but no response expected.
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._post_request(payload, timeout=self.CALL_TIMEOUT_S)
        except Exception:
            pass

    def call_tool(self, tool_name: str, args: dict) -> str:
        if not self._initialized:
            return f"MCP error: server `{self.name}` not initialized"
        try:
            result = self._request("tools/call", {
                "name": tool_name,
                "arguments": args or {},
            }, timeout=self.CALL_TIMEOUT_S)
        except HttpMCPError as e:
            return f"MCP error: {e}"
        return _extract_text(result)

    def close(self) -> None:
        # Nothing to clean up for a stateless HTTP client. Subclasses that
        # hold a persistent SSE connection override this.
        self._initialized = False


class HttpMCPClient(_RemoteMCPClientBase):
    """Streamable HTTP transport. POST each JSON-RPC request to ``url``.

    Handles three response shapes:
    - ``application/json``           → body is the JSON-RPC response.
    - ``text/event-stream``          → first ``data:`` payload is the
      response (others ignored; we don't subscribe to server-initiated
      notifications in this minimal client).
    - ``application/json-seq``       → first JSON line is the response.
    """

    def _post_request(self, payload: dict, *, timeout: float) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read(2_000_000)
        except urllib.error.HTTPError as e:
            raise HttpMCPError(
                f"MCP server `{self.name}`: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise HttpMCPError(
                f"MCP server `{self.name}`: network error: {e.reason}") from e
        return _parse_remote_response(raw, ctype, payload.get("id"))


class SseMCPClient(_RemoteMCPClientBase):
    """Legacy SSE transport. GET ``url`` opens the event stream; the
    server emits an ``endpoint`` event telling us where to POST
    JSON-RPC. We then run the same request/response cycle as HTTP.

    This supports the older MCP spec still used by some servers; new
    deployments should prefer ``HttpMCPClient``.
    """

    def __init__(self, name: str, url: str, *,
                 headers: dict[str, str] | None = None):
        super().__init__(name, url, headers=headers)
        # The POST endpoint is learned from the first ``endpoint`` event.
        self._post_url: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._endpoint_event = threading.Event()
        # id → response dict for in-flight requests.
        self._pending: dict[int, list] = {}
        self._response_lock = threading.Lock()
        self._reader_error: str | None = None
        # Base URL for resolving relative endpoint paths.
        self._base_url = url.rstrip("/")

    def startup(self) -> None:
        if self._initialized:
            return
        # Open the SSE stream; reader thread keeps it alive for the
        # lifetime of the client (server pushes responses + notifications
        # down the same stream).
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"mcp-sse-{self.name}",
            daemon=True,
        )
        self._reader_thread.start()
        if not self._endpoint_event.wait(timeout=self.STARTUP_TIMEOUT_S):
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE endpoint event timeout")
        if self._reader_error:
            raise HttpMCPError(self._reader_error)
        super().startup()

    def _resolve_endpoint(self, data: str) -> str:
        data = data.strip()
        if data.startswith("http://") or data.startswith("https://"):
            return data
        if not data.startswith("/"):
            data = "/" + data
        # Strip the path of the SSE URL, keep scheme+host.
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(self._base_url)
        return urlunsplit((parts.scheme, parts.netloc, data, "", ""))

    def _reader_loop(self) -> None:
        req = urllib.request.Request(self.url, headers={
            **self.headers,
            "Accept": "text/event-stream",
        }, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except Exception as e:
            self._reader_error = (
                f"SSE connect failed: {type(e).__name__}: {e}")
            self._endpoint_event.set()
            return
        try:
            event_type: str | None = None
            data_lines: list[str] = []
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    # Event boundary.
                    if event_type is not None and data_lines:
                        self._handle_event(event_type, "\n".join(data_lines))
                    event_type = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue  # comment / keep-alive
                if ":" in line:
                    field, _, value = line.partition(":")
                    value = value.lstrip(" ")
                    if field == "event":
                        event_type = value
                    elif field == "data":
                        data_lines.append(value)
            # Stream closed by server.
            self._reader_error = "SSE stream closed by server"
        except Exception as e:
            self._reader_error = f"SSE reader: {type(e).__name__}: {e}"
        finally:
            # Wake any stragglers.
            self._endpoint_event.set()
            with self._response_lock:
                for slot in self._pending.values():
                    slot[0].set()

    def _handle_event(self, event_type: str, data: str) -> None:
        if event_type == "endpoint" and self._post_url is None:
            self._post_url = self._resolve_endpoint(data)
            self._endpoint_event.set()
            return
        # Default: treat as a JSON-RPC response payload.
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if msg_id is None:
            return
        with self._response_lock:
            slot = self._pending.get(msg_id)
            if slot is None:
                return
            slot[1] = {
                "result": msg.get("result"),
                "error": msg.get("error"),
            }
            slot[0].set()

    def _post_request(self, payload: dict, *, timeout: float) -> dict:
        if self._post_url is None:
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE endpoint not yet known")
        if self._reader_error:
            raise HttpMCPError(self._reader_error)
        msg_id = payload.get("id")
        is_notify = msg_id is None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        post_headers = dict(self.headers)
        post_headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._post_url, data=body, headers=post_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(1_000_000)  # drain; SSE responses come back over the stream
        except urllib.error.HTTPError as e:
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE POST HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE POST network error: {e.reason}") from e
        if is_notify:
            return {}
        if msg_id is None:
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE response without id")
        slot: list = [threading.Event(), None]
        with self._response_lock:
            self._pending[int(msg_id)] = slot
        if not slot[0].wait(timeout=timeout):
            with self._response_lock:
                self._pending.pop(int(msg_id), None)
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE response timeout for "
                f"id={msg_id}")
        with self._response_lock:
            self._pending.pop(int(msg_id), None)
        outcome = slot[1]
        if not outcome:
            raise HttpMCPError(
                f"MCP server `{self.name}`: SSE reader closed before response")
        return outcome

    def close(self) -> None:
        self._initialized = False
        # Reader thread is a daemon; the open SSE socket will be reaped
        # when the process exits. There's no portable way to interrupt a
        # blocking ``urllib`` read in Python without spawning a real
        # HTTP client, which we keep out of deps on purpose.


def _parse_remote_response(raw: bytes, content_type: str,
                           expected_id: Any) -> dict:
    """Extract a JSON-RPC response from any of the three Streamable
    HTTP response shapes."""
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type or "application/json-seq" in content_type:
        # SSE framing: blank-line separated ``event:``/``data:`` blocks.
        # We want the first block whose decoded ``data`` JSON carries our id.
        if "text/event-stream" in content_type:
            for block in text.split("\n\n"):
                data_lines = [ln[5:] if ln.startswith("data:") else ln
                              for ln in block.splitlines()
                              if ln.startswith("data:")]
                if not data_lines:
                    continue
                payload = "\n".join(data_lines).strip()
                if not payload:
                    continue
                try:
                    msg = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == expected_id:
                    return msg
            # Fall back to first parsable data line.
            for block in text.split("\n\n"):
                data_lines = [ln[5:] if ln.startswith("data:") else ln
                              for ln in block.splitlines()
                              if ln.startswith("data:")]
                if data_lines:
                    try:
                        msg = json.loads("\n".join(data_lines).strip())
                        if isinstance(msg, dict):
                            return msg
                    except json.JSONDecodeError:
                        continue
        else:  # application/json-seq
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    return msg
        raise HttpMCPError("Streamable HTTP response had no parseable message")
    # Plain JSON response.
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise HttpMCPError(f"Streamable HTTP returned non-JSON: {e}") from e
    if not isinstance(msg, dict):
        raise HttpMCPError("Streamable HTTP returned non-object JSON")
    return msg


__all__ = ["HttpMCPClient", "HttpMCPError", "SseMCPClient"]
