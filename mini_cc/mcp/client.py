"""Per-project MCP client pool.

Ports s20 lines 1531-1646 from s20_comprehensive/code.py with these changes:
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

    def _record_attempt(self, name: str, ok: bool, message: str) -> None:
        self._attempts[name] = AttemptRecord(name=name, ok=ok, message=message)

    def list_attempts(self) -> dict[str, AttemptRecord]:
        """Return every recorded connect attempt keyed by server name."""
        return dict(self._attempts)

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

    def disconnect(self, name: str) -> bool:
        return self._clients.pop(name, None) is not None

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
