"""MCP consumer transport — real stdio JSON-RPC subprocess.

Spawns an external MCP server (a Node or Python process speaking the
Model Context Protocol over its stdin/stdout), runs the initialize +
tools/list handshake, and exposes the discovered tools through the
existing ``MCPClient`` interface so ``MCPPool.all_tools()`` picks them
up automatically.

Wire format (per MCP spec):
- Each JSON-RPC message is framed with a ``Content-Length: <n>`` header
  followed by ``\r\n\r\n`` and the JSON body, encoded as UTF-8.
- Requests carry an incrementing numeric id; responses are matched on
  that id.
- Notifications (no id) flow one-way from server to client (e.g.
  ``notifications/initialized``); we don't expect any in this minimal
  client, but the reader tolerates them.

Designed to be test-friendly: pass any object with ``stdin.write`` /
``stdout.read`` and a ``terminate()`` method as ``proc`` to avoid
spawning real subprocesses in tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from typing import Any

from .client import MCPClient


class StdioMCPError(RuntimeError):
    """Raised when the MCP subprocess misbehaves or dies."""


class StdioMCPClient(MCPClient):
    """MCPClient backed by a child process speaking MCP over stdio.

    Lifecycle:
        client = StdioMCPClient("docs", command=["npx", "mcp-server-docs"])
        client.startup()          # initialize + tools/list
        client.call_tool("search", {"q": "..."})
        client.close()            # terminate subprocess

    The class also accepts a pre-spawned ``proc`` for tests. Any object
    with ``stdin`` (file-like with .write / .flush), ``stdout`` (with
    .readline), ``poll`` / ``wait`` and ``terminate`` works.
    """

    STARTUP_TIMEOUT_S = 10.0
    CALL_TIMEOUT_S = 30.0

    def __init__(self, name: str, command: list[str] | tuple[str, ...],
                 *,
                 env: dict[str, str] | None = None,
                 cwd: str | None = None,
                 proc: Any | None = None):
        super().__init__(name)
        # Reject empty / non-list commands early so a misconfigured
        # servers dict can't reach subprocess.Popen with garbage.
        if not isinstance(command, (list, tuple)) or not command:
            raise StdioMCPError(
                f"MCP server `{name}`: command must be a non-empty list")
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self._proc = proc
        self._next_id = 1
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        # id → (Event, result-or-error) for in-flight requests.
        self._pending: dict[int, list] = {}
        self._reader_error: str | None = None
        self._initialized = False

    # ── Subprocess bootstrap ──────────────────────────────────────────
    def _ensure_proc(self):
        if self._proc is not None:
            return
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=self.cwd,
                text=False,           # binary; we encode/decode ourselves
                bufsize=0,
            )
        except FileNotFoundError as e:
            raise StdioMCPError(
                f"MCP server `{self.name}`: command not found: "
                f"{self.command[0]!r}") from e
        except OSError as e:
            raise StdioMCPError(
                f"MCP server `{self.name}`: failed to spawn: {e}") from e

    def startup(self) -> None:
        """Run the MCP initialize + tools/list handshake.

        Populates ``self.tools`` with the server's advertised tool
        definitions (already in the ``{name, description, inputSchema}``
        shape that ``MCPPool.all_tools`` expects).
        """
        if self._initialized:
            return
        self._ensure_proc()
        assert self._proc is not None and self._proc.stdout is not None
        # Start the background reader that demuxes responses.
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"mcp-reader-{self.name}",
            daemon=True,
        )
        self._reader_thread.start()

        # initialize
        init_result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini_cc", "version": "0.1.0"},
        }, timeout=self.STARTUP_TIMEOUT_S)
        # Notify initialized (no response expected).
        try:
            self._notify("notifications/initialized", {})
        except Exception:
            # Best-effort; some servers tolerate its absence.
            pass

        # tools/list
        tools_result = self._request("tools/list", {},
                                     timeout=self.STARTUP_TIMEOUT_S)
        raw_tools = (tools_result or {}).get("tools", []) if isinstance(
            tools_result, dict) else []
        # Normalize: ensure each tool has inputSchema (defaults to empty
        # object so the agent side gets a valid JSON-schema).
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
        self._handlers = {}  # force call_tool through _request
        self._initialized = True

    # ── JSON-RPC plumbing ─────────────────────────────────────────────
    def _write_message(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _reader_loop(self) -> None:
        """Single reader thread that demuxes responses onto waiters."""
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            while True:
                # Parse framed message.
                length = self._read_content_length(stream)
                if length is None:
                    break  # EOF
                body = stream.read(length)
                if not body or len(body) < length:
                    break  # truncated / EOF
                try:
                    msg = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._dispatch(msg)
        except Exception as e:
            self._reader_error = f"reader: {type(e).__name__}: {e}"
        # Wake any stragglers.
        for slot in list(self._pending.values()):
            if slot[0] is not None:
                slot[0].set()
                slot[1] = slot[1] or {"error": "reader terminated"}

    @staticmethod
    def _read_content_length(stream) -> int | None:
        """Read CRLF-terminated headers up to the blank line; return the
        Content-Length value or None on EOF."""
        length = None
        while True:
            line = stream.readline()
            if not line:
                return None  # EOF
            # Allow either CRLF or LF terminators; tolerant reader.
            line_s = line.decode("ascii", errors="replace").strip()
            if line_s == "":
                break  # end of headers
            if ":" in line_s:
                k, _, v = line_s.partition(":")
                if k.strip().lower() == "content-length":
                    try:
                        length = int(v.strip())
                    except ValueError:
                        length = None
        return length

    def _dispatch(self, msg: dict) -> None:
        """Route one decoded JSON-RPC message to its waiter (if any)."""
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if msg_id is None:
            return  # notification; we don't subscribe to any
        slot = self._pending.get(msg_id)
        if slot is None:
            return
        slot[1] = {
            "result": msg.get("result"),
            "error": msg.get("error"),
        }
        slot[0].set()

    def _request(self, method: str, params: dict, *, timeout: float) -> Any:
        if self._reader_error:
            raise StdioMCPError(
                f"MCP server `{self.name}`: reader not running "
                f"({self._reader_error})")
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
        event = threading.Event()
        slot: list = [event, None]
        self._pending[req_id] = slot
        payload = {"jsonrpc": "2.0", "id": req_id,
                   "method": method, "params": params}
        try:
            self._write_message(payload)
        except (BrokenPipeError, OSError) as e:
            self._pending.pop(req_id, None)
            raise StdioMCPError(
                f"MCP server `{self.name}`: write failed: {e}") from e
        if not event.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            raise StdioMCPError(
                f"MCP server `{self.name}`: timeout calling `{method}`")
        result = self._pending.pop(req_id, None)
        outcome = result[1] if result else None
        if not outcome:
            raise StdioMCPError(
                f"MCP server `{self.name}`: no response for `{method}`")
        if outcome.get("error"):
            err = outcome["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise StdioMCPError(
                f"MCP server `{self.name}`: `{method}` error: {msg}")
        return outcome.get("result")

    def _notify(self, method: str, params: dict) -> None:
        self._write_message({"jsonrpc": "2.0", "method": method,
                             "params": params})

    # ── Tool invocation ───────────────────────────────────────────────
    def call_tool(self, tool_name: str, args: dict) -> str:
        """Invoke ``tools/call`` on the subprocess and return the textual
        content of the first content block (matches Anthropic tool_result
        semantics). Errors come back prefixed with ``MCP error:``."""
        if not self._initialized:
            return f"MCP error: server `{self.name}` not initialized"
        try:
            result = self._request("tools/call", {
                "name": tool_name,
                "arguments": args or {},
            }, timeout=self.CALL_TIMEOUT_S)
        except StdioMCPError as e:
            return f"MCP error: {e}"
        return _extract_text(result)

    # ── Teardown ──────────────────────────────────────────────────────
    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        finally:
            self._proc = None
            self._initialized = False


def _extract_text(result: Any) -> str:
    """Pull the textual payload out of an MCP tools/call result.

    MCP wraps tool output in ``{content: [{type: "text", text: "..."}]}``.
    We concatenate every text block; non-text blocks (image, etc.) get a
    placeholder so the agent knows something came back.
    """
    if not isinstance(result, dict):
        return str(result or "")
    content = result.get("content")
    if isinstance(content, list) and content:
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
                else:
                    chunks.append(f"[{block.get('type', 'block')}]")
            else:
                chunks.append(str(block))
        return "\n".join(chunks)
    if isinstance(content, str):
        return content
    # Some servers return a bare ``result`` string instead of content.
    if "result" in result and isinstance(result["result"], str):
        return result["result"]
    return json.dumps(result, ensure_ascii=False)


__all__ = ["StdioMCPClient", "StdioMCPError"]
