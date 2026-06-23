"""Language Server Protocol (LSP) manager + tool.

mini_cc's LSP integration is intentionally simple: a per-project
LSPManager lazily boots language servers (one per language), keeps
them alive across tool calls, and exposes the common navigation ops
(goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol,
goToImplementation, prepareCallHierarchy, incomingCalls, outgoingCalls).

Server binaries are resolved per-language from a registry; out of the
box we probe PATH for ``pyright``, ``pylsp``, ``clangd``, ``typescript-language-server``
etc. Projects can override the registry via LSPManager(language_servers=...).

The implementation speaks just enough JSON-RPC over stdio to drive a
server — no dependency on the optional ``pygls`` package. Each request
is a single idempotent round-trip; we don't rely on persistent document
state (servers should fall back to disk reads if a file isn't open).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..tools.base import FunctionTool, ToolContext


# Default language → server-binary candidates. First match on PATH wins.
DEFAULT_LANGUAGE_SERVERS: dict[str, list[str]] = {
    "python": ["pyright-langserver", "pylsp", "jedi-language-server"],
    "javascript": ["typescript-language-server", "vtsls"],
    "typescript": ["typescript-language-server", "vtsls"],
    "c": ["clangd"],
    "cpp": ["clangd"],
    "c++": ["clangd"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
    "java": ["jdtls"],
    "ruby": ["solargraph"],
}


# Standard LSP operations exposed by the tool. Each maps to the matching
# JSON-RPC method name. Call-hierarchy ops are nested: prepare first,
# then incoming/outgoing against the prepared item.
OPERATIONS = {
    "goToDefinition": "textDocument/definition",
    "goToImplementation": "textDocument/implementation",
    "findReferences": "textDocument/references",
    "hover": "textDocument/hover",
    "documentSymbol": "textDocument/documentSymbol",
    "workspaceSymbol": "workspace/symbol",
    "prepareCallHierarchy": "textDocument/prepareCallHierarchy",
    "incomingCalls": "callHierarchy/incomingCalls",
    "outgoingCalls": "callHierarchy/outgoingCalls",
}


@dataclass
class _ServerHandle:
    """One live LSP subprocess + its reader thread."""
    proc: subprocess.Popen
    language: str
    binary: str
    # Lock serializes requests — the JSON-RPC wire protocol is
    # synchronous per-server; we don't multiplex concurrent requests
    # because most language servers serialize them anyway.
    lock: threading.Lock = field(default_factory=threading.Lock)
    _next_id: int = 1
    initialized: bool = False

    def next_id(self) -> int:
        with self.lock:
            i = self._next_id
            self._next_id += 1
            return i


class LSPManager:
    """Per-project LSP server pool.

    Lazily starts a server the first time a file with a given language
    is queried. Servers stay alive until the manager is shut down.
    """

    def __init__(self, project_root: Path,
                 language_servers: dict[str, list[str]] | None = None):
        self.project_root = Path(project_root).resolve()
        self.language_servers = language_servers or DEFAULT_LANGUAGE_SERVERS
        self._servers: dict[str, _ServerHandle] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────
    def request(self, language: str, method: str,
                params: dict | None = None, *, timeout: float = 10.0) -> Any:
        """Send a single LSP request. Returns the JSON-RPC ``result``.

        Raises RuntimeError if the language has no configured server or
        the server can't be started. Timeouts raise TimeoutError.
        """
        handle = self._get_or_start(language)
        if handle is None:
            raise RuntimeError(
                f"no LSP server binary found on PATH for language '{language}'. "
                f"Tried: {self.language_servers.get(language, [])}")
        with handle.lock:
            if not handle.initialized:
                self._initialize(handle)
            msg_id = handle.next_id()
            self._send_request(handle, msg_id, method, params)
            return self._read_response(handle, msg_id, timeout)

    def shutdown(self) -> None:
        """Politely shut down every running server. Safe to call twice."""
        with self._lock:
            handles = list(self._servers.values())
            self._servers.clear()
        for h in handles:
            try:
                self._send_request(h, h.next_id(), "shutdown", None)
                self._send_notification(h, "exit", None)
            except Exception:
                pass
            try:
                h.proc.terminate()
                h.proc.wait(timeout=2)
            except Exception:
                try:
                    h.proc.kill()
                except Exception:
                    pass

    # ── Language / file plumbing ──────────────────────────────────────
    @staticmethod
    def language_for_file(path: str) -> str | None:
        """Return the language id for a file based on extension."""
        ext = Path(path).suffix.lower().lstrip(".")
        return _EXT_TO_LANGUAGE.get(ext)

    # ── Internal helpers ──────────────────────────────────────────────
    def _get_or_start(self, language: str) -> _ServerHandle | None:
        with self._lock:
            h = self._servers.get(language)
            if h is not None:
                return h
        binary = self._find_binary(language)
        if binary is None:
            return None
        handle = self._launch(binary, language)
        if handle is None:
            return None
        with self._lock:
            # Race: another thread may have launched one too. Keep the
            # first; shut the loser.
            existing = self._servers.get(language)
            if existing is not None:
                try:
                    handle.proc.terminate()
                except Exception:
                    pass
                return existing
            self._servers[language] = handle
            return handle

    def _find_binary(self, language: str) -> str | None:
        for candidate in self.language_servers.get(language, []):
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def _launch(self, binary: str, language: str) -> _ServerHandle | None:
        # Most LSP servers accept ``--stdio``. clangd uses --stdio=9999
        # historically but modern clangd accepts --log-level too; the
        # common surface is just ``--stdio``.
        args = self._launch_args(binary, language)
        try:
            proc = subprocess.Popen(
                [binary, *args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                # JSON-RPC over stdio is binary; encoding=None keeps bytes.
                bufsize=0)
        except Exception:
            return None
        return _ServerHandle(proc=proc, language=language, binary=binary)

    @staticmethod
    def _launch_args(binary: str, language: str) -> list[str]:
        # Per-binary quirks. Keep conservative defaults.
        name = Path(binary).name
        if "pyright" in name:
            return ["--stdio"]
        if "clangd" in name:
            return ["--stdio"]
        if "typescript-language-server" in name or "vtsls" in name:
            return ["--stdio"]
        if "rust-analyzer" in name or "gopls" in name or "jdtls" in name:
            return []
        if "solargraph" in name:
            return ["stdio"]
        if "pylsp" in name or "jedi" in name:
            return []
        # Default: most LSP servers accept --stdio.
        return ["--stdio"]

    def _initialize(self, handle: _ServerHandle) -> None:
        params = {
            "processId": os.getpid(),
            "rootUri": _path_to_uri(self.project_root),
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "implementation": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"dynamicRegistration": False},
                    "documentSymbol": {"dynamicRegistration": False},
                    "prepareCallHierarchy": {"dynamicRegistration": False},
                },
                "workspace": {
                    "symbol": {"dynamicRegistration": False},
                },
            },
        }
        msg_id = handle.next_id()
        self._send_request(handle, msg_id, "initialize", params)
        # Wait for the init response (default timeout).
        self._read_response(handle, msg_id, timeout=15.0)
        self._send_notification(handle, "initialized", {})
        handle.initialized = True

    def _send_request(self, h: _ServerHandle, msg_id: int,
                      method: str, params: Any) -> None:
        self._write_message(h, {
            "jsonrpc": "2.0", "id": msg_id,
            "method": method, "params": params or {},
        })

    def _send_notification(self, h: _ServerHandle, method: str,
                           params: Any) -> None:
        self._write_message(h, {
            "jsonrpc": "2.0", "method": method, "params": params or {},
        })

    def _write_message(self, h: _ServerHandle, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        if h.proc.stdin is None:
            raise RuntimeError("server stdin closed")
        h.proc.stdin.write(header + body)
        h.proc.stdin.flush()

    def _read_response(self, h: _ServerHandle, expected_id: int,
                       timeout: float) -> Any:
        # Each iteration reads one Content-Length framed message. We
        # ignore server-initiated notifications (window/showMessage etc.)
        # and keep reading until the matching response id arrives.
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            msg = self._read_one_message(h, deadline)
            if msg is None:
                break
            if msg.get("id") == expected_id:
                if "error" in msg:
                    raise RuntimeError(
                        f"LSP error: {json.dumps(msg['error'])[:200]}")
                return msg.get("result")
            # Not our response — drop it (server notifications, async).
        raise TimeoutError(
            f"LSP response for id={expected_id} not received within {timeout}s")

    def _read_one_message(self, h: _ServerHandle, deadline: float) -> dict | None:
        # Read headers (Content-Length: NNN\r\n...\r\n\r\n), then body.
        if h.proc.stdout is None:
            return None
        headers: dict[str, str] = {}
        while True:
            line = h.proc.stdout.readline()
            if not line:
                return None
            line_s = line.decode("ascii", errors="replace").strip()
            if not line_s:
                break
            if ":" in line_s:
                k, _, v = line_s.partition(":")
                headers[k.strip().lower()] = v.strip()
        try:
            n = int(headers.get("content-length", "0"))
        except ValueError:
            return None
        if n <= 0:
            return None
        body = h.proc.stdout.read(n)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None


# File extension → language id. Keep modest; users can extend by
# passing language_servers= into LSPManager.
_EXT_TO_LANGUAGE: dict[str, str] = {
    "py": "python",
    "js": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "c": "c", "h": "c",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "rb": "ruby",
}


def _path_to_uri(p: Path) -> str:
    # file:// URI with proper percent-encoding for spaces etc.
    abs_p = p.resolve()
    # Path.as_uri() does this correctly per platform.
    return abs_p.as_uri()


def _line_col_to_pos(line: int, character: int) -> dict:
    return {"line": int(line), "character": int(character)}


def _text_document_position(filePath: str, line: int, character: int) -> dict:
    return {
        "textDocument": {"uri": Path(filePath).resolve().as_uri()},
        "position": _line_col_to_pos(line, character),
    }


# ── Tool handler ─────────────────────────────────────────────────────────

def _lsp(ctx: ToolContext, args: dict) -> str:
    """Dispatch a single LSP operation. Returns formatted text."""
    operation = (args.get("operation") or "").strip()
    if operation not in OPERATIONS:
        return (f"Error: unknown operation '{operation}'. "
                f"Choose from: {', '.join(sorted(OPERATIONS))}")
    file_path = (args.get("filePath") or "").strip()
    if not file_path and operation != "workspaceSymbol":
        return "Error: filePath is required for this operation"

    # Resolve through the sandbox so project-rooted relative paths work.
    sandbox = ctx.sandbox
    abs_path = sandbox.resolve_path(file_path) if file_path else None
    if abs_path is not None:
        try:
            sandbox.validate_path(abs_path)
        except Exception as e:
            return f"Error: path validation failed: {e}"
        abs_path_str = str(abs_path)
    else:
        abs_path_str = ""

    language = (args.get("language") or "").strip().lower()
    if not language and abs_path is not None:
        language = LSPManager.language_for_file(abs_path_str) or ""
    if not language:
        return (f"Error: could not infer language from {file_path!r}. "
                "Pass `language` explicitly.")

    manager = _get_manager(ctx)
    if manager is None:
        return ("Error: no LSPManager attached to this project. "
                "Set project.lsp_manager to enable code intelligence.")

    try:
        params = _build_params(operation, args, abs_path_str)
    except Exception as e:
        return f"Error: bad params: {e}"

    method = OPERATIONS[operation]
    try:
        result = manager.request(language, method, params, timeout=15.0)
    except TimeoutError as e:
        return f"Error: LSP request timed out: {e}"
    except RuntimeError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

    return _format_result(operation, result)


def _build_params(operation: str, args: dict, abs_path: str) -> dict:
    """Translate tool args into the LSP method's params object."""
    if operation == "workspaceSymbol":
        query = (args.get("query") or "").strip()
        return {"query": query}
    line = int(args.get("line", 0))
    character = int(args.get("character", 0))
    if operation == "documentSymbol":
        return {"textDocument": {"uri": Path(abs_path).resolve().as_uri()}}
    if operation in ("prepareCallHierarchy",):
        return _text_document_position(abs_path, line, character)
    if operation in ("incomingCalls", "outgoingCalls"):
        # Caller must pass the prepared item as JSON; we extract the
        # minimal required fields.
        item = args.get("item") or {}
        return {"item": {
            "name": item.get("name", ""),
            "kind": item.get("kind", 12),
            "uri": item.get("uri") or Path(abs_path).resolve().as_uri(),
            "range": item.get("range")
            or {"start": _line_col_to_pos(int(args.get("line", 0)), 0),
                "end": _line_col_to_pos(int(args.get("line", 0)), 0)},
            "selectionRange": item.get("selectionRange")
            or {"start": _line_col_to_pos(int(args.get("line", 0)), 0),
                "end": _line_col_to_pos(int(args.get("line", 0)), 0)},
        }}
    # goToDefinition / findReferences / hover / goToImplementation
    return _text_document_position(abs_path, line, character)


def _format_result(operation: str, result: Any) -> str:
    """Render the LSP response as compact text the LLM can quote from."""
    if result is None:
        return f"[{operation}] _no result_"
    lines = [f"**{operation}**", ""]
    if operation == "hover":
        # Result is {contents: ...} or MarkupContent.
        contents = result.get("contents") if isinstance(result, dict) else None
        if isinstance(contents, dict):
            value = contents.get("value", "")
        elif isinstance(contents, list):
            value = "\n".join(_hover_part_to_str(c) for c in contents)
        else:
            value = str(contents or "")
        lines.append(value.strip() or "_empty hover_")
        return "\n".join(lines)
    if operation == "documentSymbol":
        for sym in result if isinstance(result, list) else [result]:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name", "?")
            kind = _symbol_kind_name(sym.get("kind", 0))
            detail = sym.get("detail") or ""
            lines.append(f"- `{name}` ({kind}){f' — {detail}' if detail else ''}")
        return "\n".join(lines) if len(lines) > 2 else f"[{operation}] _no symbols_"
    if operation == "workspaceSymbol":
        for sym in result if isinstance(result, list) else [result]:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name", "?")
            container = sym.get("containerName", "")
            loc = sym.get("location", {})
            uri = loc.get("uri", "") if isinstance(loc, dict) else ""
            lines.append(f"- `{name}`"
                         + (f" ({container})" if container else "")
                         + (f" — {uri}" if uri else ""))
        return "\n".join(lines) if len(lines) > 2 else f"[{operation}] _no matches_"
    # Definition / implementation / references: list of Location objects.
    locations = result if isinstance(result, list) else [result]
    found = False
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        found = True
        uri = loc.get("uri", "")
        rng = loc.get("range", {}) or {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        line = start.get("line", "?") if isinstance(start, dict) else "?"
        char = start.get("character", "?") if isinstance(start, dict) else "?"
        lines.append(f"- `{uri}` {line}:{char}")
    return "\n".join(lines) if found else f"[{operation}] _no locations_"


def _hover_part_to_str(part: Any) -> str:
    if isinstance(part, dict):
        return part.get("value", "") or part.get("language", "")
    return str(part)


# Numeric LSP SymbolKind → readable name. Subset; unknown falls back to
# the raw number.
_SYMBOL_KINDS: dict[int, str] = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct",
    24: "Event", 25: "Operator", 26: "TypeParameter",
}


def _symbol_kind_name(k: int) -> str:
    return _SYMBOL_KINDS.get(k, f"kind={k}")


def _get_manager(ctx: ToolContext):
    """Pull the LSPManager off the project, if one is attached.

    Accepts either a real LSPManager instance or any duck-typed object
    exposing ``request(language, method, params, *, timeout=...)`` —
    useful for tests and for projects that want to wrap the manager
    with caching/telemetry without subclassing.
    """
    project = getattr(ctx, "project_ref", None)
    if project is None:
        return None
    mgr = getattr(project, "lsp_manager", None)
    if mgr is None:
        return None
    if isinstance(mgr, LSPManager):
        return mgr
    if hasattr(mgr, "request") and callable(getattr(mgr, "request")):
        return mgr
    return None


LSP_TOOL = FunctionTool(
    name="lsp",
    description=(
        "Code intelligence via Language Server Protocol. Supports 9 "
        "operations: goToDefinition, goToImplementation, findReferences, "
        "hover, documentSymbol, workspaceSymbol, prepareCallHierarchy, "
        "incomingCalls, outgoingCalls. Lines and characters are 0-based "
        "(LSP convention). The right language server is picked from the "
        "file extension unless `language` is given explicitly. Requires "
        "an LSP server binary on PATH (pyright, clangd, tsserver, etc.)."),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(OPERATIONS.keys()),
                "description": "LSP operation to perform.",
            },
            "filePath": {
                "type": "string",
                "description": "Absolute or project-relative file path. "
                               "Required for all operations except workspaceSymbol.",
            },
            "line": {
                "type": "integer",
                "description": "0-based line number (LSP convention).",
            },
            "character": {
                "type": "integer",
                "description": "0-based character offset in the line.",
            },
            "language": {
                "type": "string",
                "description": "Override language id (e.g. 'python'). "
                               "Inferred from extension if omitted.",
            },
            "query": {
                "type": "string",
                "description": "Search query for workspaceSymbol.",
            },
            "item": {
                "type": "object",
                "description": "Prepared call-hierarchy item (output of "
                               "prepareCallHierarchy) for incomingCalls / "
                               "outgoingCalls.",
            },
        },
        "required": ["operation"],
    },
    fn=_lsp,
)


ALL = [LSP_TOOL]
