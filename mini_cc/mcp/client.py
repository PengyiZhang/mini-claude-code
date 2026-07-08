"""Per-project MCP client pool.

Ports s20 lines 1531-1646 from reference/learn-claude-code/s20_comprehensive/code.py with these changes:
- No module-global mcp_clients dict — instance state on MCPPool
- Server factories are registered on a class-level registry (app setup),
  per-project MCPPool holds the connected clients
- Tool wrappers expose MCP tools as FunctionTool instances so they merge
  transparently with builtin tools in the AgentLoop

Server factory = Callable[[], MCPClient]. Register at app startup:
    MCPPool.register_factory("docs", lambda: MCPClient("docs", [...], {...}))
Then per project:
    pool = MCPPool(project_id)
    pool.connect("docs")
    pool.all_tools()  # -> [FunctionTool, ...] with names like mcp__docs__search
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED.sub("_", name)


@dataclass
class AttemptRecord:
    """Outcome of one connect attempt — kept so ``/mcp`` can surface
    servers that were discovered on disk but failed to connect (auth
    error, unreachable host, bad handshake, …). Without this a failed
    server would silently vanish from both ``connected`` and the
    ``connectable`` factory list, leaving the user staring at "no MCP
    servers registered" with no clue why."""
    name: str
    ok: bool
    message: str


class MCPClient:
    """Discovers and calls tools on an MCP server.

    This is the in-process variant (the SDK's actual MCP transport lives
    elsewhere; this matches s20's teaching abstraction and is the right
    seam for swapping in a real MCP client later).
    """

    def __init__(self, name: str,
                 tool_defs: list[dict] | None = None,
                 handlers: dict[str, Callable] | None = None):
        self.name = name
        self.tools: list[dict] = tool_defs or []
        self._handlers: dict[str, Callable] = handlers or {}

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, Callable]) -> None:
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**(args or {}))
        except Exception as e:
            return f"MCP error: {type(e).__name__}: {e}"


class MCPPool:
    """Per-project MCP connection pool.

    Server factories live on the class so they're shared across all
    projects; connection state (which servers a project has connected to)
    is per-instance.
    """

    _factories: dict[str, Callable[[], MCPClient]] = {}

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._clients: dict[str, MCPClient] = {}
        # name -> last outcome of a connect attempt. Populated by every
        # connect_* path so /mcp can show discovered-but-failed servers.
        self._attempts: dict[str, AttemptRecord] = {}
        # name -> spec dict for servers connected via connect_from_spec
        # OR connect_stdio/connect_http/connect_sse (we synthesize a spec
        # from the call args). Kept across disconnect so /mcp can show a
        # "disconnected" row and reconnect() has the data it needs
        # (debug.8 Task B). Pre-fix, disconnect dropped the entry
        # entirely and reconnect failed with "Unknown server".
        self._specs: dict[str, dict] = {}
        # In-flight async connect names. Tracked separately from
        # _attempts so /mcp can render a 'connecting' badge while a
        # server warms up in the background; the entry moves out of
        # here the moment the background thread finishes (success or
        # failure) and into _clients / _attempts as usual.
        self._connecting: set[str] = set()
        # Guards _clients / _attempts / _specs / _connecting mutations.
        # Connect dispatch happens off-thread now, so the pool's state
        # dicts can't stay lock-free.
        self._lock = threading.RLock()
        # Daemon pool: connect attempts are I/O-bound (subprocess spawn,
        # HTTP handshake) and bounded by the number of configured
        # servers in .mcp.json — typically <10. A small thread pool is
        # plenty; one thread per server would also work but adds
        # nothing here.
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix=f"mcp-{project_id}")

    def _record_attempt(self, name: str, ok: bool, message: str) -> None:
        with self._lock:
            self._attempts[name] = AttemptRecord(name=name, ok=ok, message=message)

    def list_attempts(self) -> dict[str, AttemptRecord]:
        """Return every recorded connect attempt keyed by server name."""
        with self._lock:
            return dict(self._attempts)

    def list_connecting(self) -> list[str]:
        """Names of servers currently mid-async-connect. Used by /mcp
        to render a 'connecting' badge while a slow server (cold npx
        download, network handshake) warms up in the background."""
        with self._lock:
            return sorted(self._connecting)

    def wait_for_connect(self, name: str, *, timeout: float) -> bool:
        """Block until ``name`` leaves _connecting. Returns True if the
        server settled (success or failure) within ``timeout``, False if
        it's still in flight. Mostly for tests; production code reads
        ``list_connecting()`` / ``list_connected()`` non-blockingly."""
        deadline = threading.Event()
        deadline.wait(0)  # no-op; placeholder for clarity
        import time as _time
        t0 = _time.monotonic()
        while _time.monotonic() - t0 < timeout:
            with self._lock:
                if name not in self._connecting:
                    return True
            _time.sleep(0.02)
        with self._lock:
            return name not in self._connecting

    # ── Factory registry (app-level) ───────────────────────────────────
    @classmethod
    def register_factory(cls, name: str,
                         factory: Callable[[], MCPClient]) -> None:
        cls._factories[name] = factory

    @classmethod
    def unregister_factory(cls, name: str) -> None:
        cls._factories.pop(name, None)

    @classmethod
    def available_servers(cls) -> list[str]:
        return sorted(cls._factories.keys())

    @classmethod
    def reset_factories(cls) -> None:
        """Test helper — clears all registered factories."""
        cls._factories.clear()

    # ── Connection state (project-level) ───────────────────────────────
    def connect(self, name: str) -> tuple[bool, str]:
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        factory = self._factories.get(name)
        if not factory:
            available = ", ".join(self._factories.keys()) or "(none)"
            return False, f"Unknown server '{name}'. Available: {available}"
        client = factory()
        self._clients[name] = client
        # debug.8 Task B: remember that this name is connectable via the
        # factory so reconnect() works after disconnect(). The "spec" for
        # a factory-registered server is a sentinel — we don't need to
        # re-discover anything, just call connect(name) again.
        self._specs[name] = {"type": "factory"}
        tool_names = [t["name"] for t in client.tools]
        msg = (f"Connected to MCP server '{name}'. "
               f"Discovered {len(client.tools)} tools: "
               f"{', '.join(tool_names)}")
        self._record_attempt(name, True, msg)
        return True, msg

    def connect_stdio(self, name: str, command: list[str],
                      *, env: dict | None = None,
                      cwd: str | None = None) -> tuple[bool, str]:
        """Spawn a real MCP subprocess and connect to it.

        Returns ``(ok, message)``. On failure the message explains the
        underlying error (command not found, handshake timeout, etc.).
        A repeated call with the same name is a no-op success.
        """
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        # Lazy import keeps the stdio transport out of the hot path for
        # projects that don't use MCP.
        from .stdio import StdioMCPClient, StdioMCPError
        try:
            client = StdioMCPClient(name, command, env=env, cwd=cwd)
            client.startup()
        except StdioMCPError as e:
            try:
                client.close()  # type: ignore[name-defined]
            except Exception:
                pass
            self._record_attempt(name, False, str(e))
            return False, str(e)
        self._clients[name] = client
        # debug.8 Task B: keep the spec for reconnect().
        self._specs[name] = {"type": "stdio", "command": command,
                             "env": env, "cwd": cwd}
        tool_names = [t["name"] for t in client.tools]
        msg = (f"Connected to MCP server '{name}' over stdio. "
               f"Discovered {len(client.tools)} tools: "
               f"{', '.join(tool_names)}")
        self._record_attempt(name, True, msg)
        return True, msg

    def connect_http(self, name: str, url: str, *,
                     headers: dict | None = None) -> tuple[bool, str]:
        """Connect to a remote MCP server using Streamable HTTP.

        Returns ``(ok, message)``. Repeated calls with the same name are
        no-op successes — same contract as :meth:`connect_stdio`.
        """
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        from .http import HttpMCPClient, HttpMCPError
        try:
            client = HttpMCPClient(name, url, headers=headers)
            client.startup()
        except HttpMCPError as e:
            try:
                client.close()  # type: ignore[name-defined]
            except Exception:
                pass
            self._record_attempt(name, False, str(e))
            return False, str(e)
        self._clients[name] = client
        # debug.8 Task B: keep the spec for reconnect().
        self._specs[name] = {"type": "http", "url": url, "headers": headers}
        tool_names = [t["name"] for t in client.tools]
        msg = (f"Connected to MCP server '{name}' over http. "
               f"Discovered {len(client.tools)} tools: "
               f"{', '.join(tool_names)}")
        self._record_attempt(name, True, msg)
        return True, msg

    def connect_sse(self, name: str, url: str, *,
                    headers: dict | None = None) -> tuple[bool, str]:
        """Connect to a remote MCP server using the legacy SSE transport."""
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        from .http import SseMCPClient, HttpMCPError
        try:
            client = SseMCPClient(name, url, headers=headers)
            client.startup()
        except HttpMCPError as e:
            try:
                client.close()  # type: ignore[name-defined]
            except Exception:
                pass
            self._record_attempt(name, False, str(e))
            return False, str(e)
        self._clients[name] = client
        # debug.8 Task B: keep the spec for reconnect().
        self._specs[name] = {"type": "sse", "url": url, "headers": headers}
        tool_names = [t["name"] for t in client.tools]
        msg = (f"Connected to MCP server '{name}' over sse. "
               f"Discovered {len(client.tools)} tools: "
               f"{', '.join(tool_names)}")
        self._record_attempt(name, True, msg)
        return True, msg

    def connect_from_spec(self, name: str, spec: dict) -> tuple[bool, str]:
        """Dispatch by transport type.

        ``spec`` is the unified shape produced by the plugin discovery
        layer: ``{"type": "stdio"|"http"|"sse", ...}``. Unknown types
        return ``(False, message)`` rather than raising so a single bad
        entry can't abort the auto-connect loop in project assembly.
        """
        spec_type = (spec or {}).get("type", "stdio")
        if spec_type == "stdio":
            command = spec.get("command") or []
            if not isinstance(command, list) or not command:
                return False, (
                    f"MCP server '{name}': stdio spec missing command list")
            return self.connect_stdio(
                name, command,
                env=spec.get("env"),
                cwd=spec.get("cwd"),
            )
        if spec_type == "http":
            url = spec.get("url")
            if not url:
                return False, f"MCP server '{name}': http spec missing url"
            return self.connect_http(name, url, headers=spec.get("headers"))
        if spec_type == "sse":
            url = spec.get("url")
            if not url:
                return False, f"MCP server '{name}': sse spec missing url"
            return self.connect_sse(name, url, headers=spec.get("headers"))
        return False, f"MCP server '{name}': unknown type {spec_type!r}"

    def connect_from_spec_async(self, name: str, spec: dict) -> tuple[bool, str]:
        """Non-blocking variant of :meth:`connect_from_spec`.

        Records ``name`` in ``_connecting`` immediately and dispatches
        the underlying connect to a background thread. Returns
        ``(True, 'connecting')`` right away so the caller (project
        assembly) can keep going — `/mcp` shows a 'connecting' badge
        until the thread settles.

        On completion the server transitions out of ``_connecting``
        into the same connected/failed state the sync path would have
        produced, so existing roster rendering keeps working once the
        connect finishes. A repeated dispatch for an already-connected
        or already-connecting name is a no-op success — same contract
        as :meth:`connect_from_spec`.
        """
        with self._lock:
            if name in self._clients:
                return True, f"MCP server '{name}' already connected"
            if name in self._connecting:
                return True, f"MCP server '{name}' already connecting"
            # Record the spec up-front so /mcp's connecting card can
            # render transport type, AND reconnect() has the data if
            # the user clicks reconnect while we're still mid-connect.
            self._specs[name] = spec
            self._connecting.add(name)
        self._executor.submit(self._async_connect_worker, name, spec)
        return True, f"MCP server '{name}' connecting in background"

    def _async_connect_worker(self, name: str, spec: dict) -> None:
        """Background connect — runs :meth:`connect_from_spec` then clears
        the connecting flag. ``connect_from_spec`` already records the
        outcome in _attempts via the underlying connect_* method; this
        just needs to release the connecting slot. The defensive
        except-clause is for surprise exceptions (bugs in the transport,
        monkeypatch glitches in tests) so a name never gets stuck in
        _connecting forever."""
        try:
            self.connect_from_spec(name, spec)
        except Exception as e:
            with self._lock:
                self._record_attempt(
                    name, False,
                    f"async connect raised: {type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._connecting.discard(name)

    def reconnect(self, name: str) -> tuple[bool, str]:
        """Re-establish a connection from the stored spec (debug.8 Task B).

        - Still-connected: no-op success.
        - Known (we have a spec): disconnect-then-connect for transport
          types, or just :meth:`connect` for factory-registered servers.
        - Unknown: helpful error listing every known server so the user
          can see what's actually available.
        """
        if name in self._clients:
            return True, f"MCP server '{name}' already connected"
        spec = self._specs.get(name)
        if spec is None:
            known = ", ".join(sorted(self._specs.keys())) or "(none)"
            return False, (f"Unknown server '{name}'. "
                           f"Available: {known}")
        # Drop any half-dead state, then re-run the connect path. The
        # transport-specific connect_* methods already record attempts
        # so /mcp's failed/disconnected/connected visibility stays
        # accurate across reconnect cycles.
        self.disconnect(name)  # no-op if not connected
        if spec.get("type") == "factory":
            return self.connect(name)
        return self.connect_from_spec(name, spec)

    def list_known_servers(self) -> list[str]:
        """Every server we have a spec for, regardless of live state
        (debug.8 Task B). Used by /mcp to render disconnected rows."""
        return sorted(self._specs.keys())

    def get_spec(self, name: str) -> dict | None:
        """Return the stored spec for ``name`` (or None). Lets /mcp
        render transport info for disconnected servers without needing
        a live client."""
        return self._specs.get(name)

    def disconnect(self, name: str) -> bool:
        """Disconnect from a server. Calls ``client.close()`` if the
        client exposes it (real stdio/http transports do; the in-process
        teaching variant doesn't) so the underlying subprocess /
        connection pool is torn down before we drop the reference.

        Without the explicit close, popping the reference leaves the
        stdio subprocess running and the http connection pool held
        until GC happens to collect them — and on long-running servers
        that may be never.

        debug.8 Task B: the spec is preserved (``self._specs``) so
        ``reconnect()`` can find it later and ``/mcp`` can show the row
        in a "disconnected" state instead of vanishing.
        """
        client = self._clients.pop(name, None)
        if client is None:
            return False
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return True

    def forget(self, name: str) -> bool:
        """Drop both the live client AND the stored spec. Use this when
        the user explicitly removes a server (not just disconnects).
        Returns True if anything was dropped."""
        had_client = self.disconnect(name)
        had_spec = self._specs.pop(name, None) is not None
        return had_client or had_spec

    def disconnect_all(self) -> None:
        """Disconnect every connected client. Used at app shutdown so
        long-running servers don't leak MCP subprocesses across process
        restarts."""
        for name in list(self._clients.keys()):
            self.disconnect(name)

    def list_connected(self) -> list[str]:
        return sorted(self._clients.keys())

    def all_tools(self):
        """Return MCP tools as FunctionTool wrappers (lazy import to avoid
        a circular tools <-> mcp dependency at module load)."""
        from ..tools.base import FunctionTool
        wrappers = []
        for server_name, client in self._clients.items():
            safe_server = normalize_mcp_name(server_name)
            for tool_def in client.tools:
                tool_name = tool_def["name"]
                safe_tool = normalize_mcp_name(tool_name)
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                description = tool_def.get("description", "")
                input_schema = tool_def.get("inputSchema",
                                            {"type": "object", "properties": {}})

                def _make_fn(c=client, t=tool_name):
                    def fn(ctx, args):
                        return c.call_tool(t, args or {})
                    return fn

                wrappers.append(FunctionTool(
                    name=prefixed,
                    description=description,
                    input_schema=input_schema,
                    fn=_make_fn(),
                ))
        return wrappers

    # Parameter names that strongly indicate "this tool takes an image".
    # Bare ``url`` is intentionally excluded — too generic (could be a
    # fetch endpoint, S3 link, etc.); the prefixed variants
    # (image_url, image_path) are safe.
    _VISION_PARAM_NAMES = frozenset({
        "image", "imagesource", "imageurl", "image_url",
        "image_path", "picture", "screenshot",
    })
    _VISION_DESC_KEYWORDS = ("image", "screenshot", "picture")

    def vision_tools(self):
        """Subset of all_tools() limited to MCP tools that look like
        they accept an image input — used to compose the vision-gated
        system prompt when the configured model lacks native vision."""
        seen = set()
        out = []
        for tool in self.all_tools():
            if tool.name in seen:
                continue
            schema = getattr(tool, "input_schema", {}) or {}
            props = set((schema.get("properties") or {}).keys())
            props_lower = {p.lower() for p in props}
            if props_lower & self._VISION_PARAM_NAMES:
                seen.add(tool.name)
                out.append(tool)
                continue
            desc = (getattr(tool, "description", "") or "").lower()
            if any(k in desc for k in self._VISION_DESC_KEYWORDS):
                seen.add(tool.name)
                out.append(tool)
        return out
