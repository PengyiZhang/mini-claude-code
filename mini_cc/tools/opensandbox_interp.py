"""OpenSandbox code interpreter adapter.

Backs the ``execute_code`` tool when ``MINI_CC_REPL_BACKEND=opensandbox``.
Goes through execd's ``/code/context`` + ``/code`` endpoints (spec:
specs/execd-api.yaml), which keep a Jupyter kernel session alive per
context — so variables, imports, and mutated module state survive
across calls. Better than ``docker exec python -c "..."`` for any
stateful workflow (data analysis, exploration, debugging).

Response stream is JSON-per-line (same framing as ``/command`` despite
the ``text/event-stream`` content-type). We parse each line as one
ServerStreamEvent dict and accumulate stdout/stderr/result text.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.request import urlopen as _stdlib_urlopen

# Re-bound at module level so tests can monkeypatch this name without
# touching stdlib internals.
urlopen = _stdlib_urlopen


def _post_stream(url: str, headers: dict, body: dict) -> bytes:
    """POST JSON to ``url`` and return raw response bytes (the stream).

    The execd API uses ``text/event-stream`` but actually emits JSON
    objects one per line. We return the raw bytes so the caller can
    split on ``\\n`` and decode each line independently."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=60) as r:
        return r.read()


def _post_json(url: str, headers: dict, body: dict) -> dict:
    """POST JSON, return parsed JSON. Used for /code/context."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


class OpenSandboxInterpreter:
    """Stateful code interpreter bound to one OpenSandbox sandbox.

    Construct with the sandbox id + execd endpoint (URL + auth headers
    resolved via the lifecycle server's /sandboxes/<id>/endpoints/<port>
    in OpenSandboxRuntime._get_execd_endpoint). The lifecycle owner
    (ContainerSandbox / TenantContainerManager) is responsible for
    ensure_running before any run() call.

    Context creation is lazy and cached per language: python and bash
    get separate Jupyter kernel sessions, both reused across calls.
    """

    def __init__(self, sandbox_id: str, execd_url: str,
                 execd_headers: dict):
        self.sandbox_id = sandbox_id
        self.execd_url = execd_url.rstrip("/")
        self.execd_headers = dict(execd_headers)
        self._contexts: dict[str, str] = {}  # language -> context_id

    def _ensure_context(self, language: str) -> str:
        cached = self._contexts.get(language)
        if cached is not None:
            return cached
        resp = _post_json(f"{self.execd_url}/code/context",
                          self.execd_headers, {"language": language})
        cid = resp["id"]
        self._contexts[language] = cid
        return cid

    def run(self, code: str, *, language: str = "python") -> str:
        """Execute ``code`` in the language's context; return text output.

        Joins stdout + stderr + result text (text/plain MIME) into one
        string for the agent. Errors surface the structured ``ename``
        and ``evalue`` so the agent can see why their code failed
        rather than getting back an empty string."""
        try:
            cid = self._ensure_context(language)
            raw = _post_stream(
                f"{self.execd_url}/code", self.execd_headers,
                {"context": {"id": cid, "language": language},
                 "code": code})
        except urllib.error.URLError as e:
            return f"Error: interpreter unreachable: {e.reason}"
        except OSError as e:
            return f"Error: interpreter unreachable: {e}"

        return _parse_code_stream(raw)


def _parse_code_stream(raw: bytes) -> str:
    """Walk execd's /code response stream → concatenated output text.

    Each line is one ServerStreamEvent JSON object. Event types we
    consume:
    - ``stdout`` / ``stderr``: append ``text``
    - ``result``: append ``results['text/plain']`` if present
    - ``error``: append ename + evalue + first traceback line
    - ``execution_complete``: terminal marker (no payload we need)
    """
    stdout: list[str] = []
    errors: list[str] = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype in ("stdout", "stderr"):
            text = evt.get("text", "")
            if text:
                stdout.append(text)
        elif etype == "result":
            results = evt.get("results") or {}
            text = results.get("text/plain")
            if text:
                stdout.append(text)
        elif etype == "error":
            err = evt.get("error") or {}
            parts = [p for p in (
                err.get("ename"), err.get("evalue")) if p]
            if parts:
                errors.append(": ".join(parts))
            tb = err.get("traceback") or []
            if tb:
                errors.append(tb[0])
    chunks = stdout + errors
    return "\n".join(chunks).strip() if chunks else "(no output)"
