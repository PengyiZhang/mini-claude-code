"""OpenSandboxRuntime: ContainerRuntime Protocol 的远端 HTTP 实现。

后端是 OpenSandbox lifecycle server (FastAPI :8080 之类) + 容器内 execd
agent (:44772)。所有调用走 stdlib urllib，不依赖 opensandbox SDK —— 与
mini_cc/mcp/http.py 风格一致，避免拖入 mcp/pydantic/async machinery 重依赖。

幂等策略：每个 mini_cc 租户对应一个 OpenSandbox sandbox，通过 metadata 字段
``mini-cc-tid=<tid>`` 索引。OpenSandbox spec 要求 metadata key 符合 DNS label
规则（首尾字母数字，中段可含 ``-_.``），故用连字符不用下划线。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.request import urlopen as _stdlib_urlopen

# Re-bound at module level so tests can monkeypatch this name without
# touching stdlib internals.
urlopen = _stdlib_urlopen


@dataclass(frozen=True)
class OpenSandboxConfig:
    base_url: str               # e.g. "https://osb.example.com:8080/v1"
    api_key: str                # 可为空（dev 模式 server 不校验）
    execd_port: int = 44772
    request_timeout: int = 30
    poll_interval: float = 0.5
    ready_timeout: int = 60     # 等待 Pending → Running 的最长时间

    @classmethod
    def from_env(cls) -> "OpenSandboxConfig":
        proto = os.environ.get("OPEN_SANDBOX_PROTOCOL", "http")
        domain = os.environ.get("OPEN_SANDBOX_DOMAIN", "localhost:8080")
        return cls(
            base_url=f"{proto}://{domain}/v1",
            api_key=os.environ.get("OPEN_SANDBOX_API_KEY", ""),
        )


def _request(cfg: OpenSandboxConfig, method: str, path: str,
             body: dict | None = None, timeout: int | None = None
             ) -> tuple[int, dict]:
    """Issue an HTTP request against the lifecycle server. Returns
    (status, parsed_json). On connection error returns (-1, {}) so callers
    can treat "server unreachable" the same as "not available"."""
    url = f"{cfg.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cfg.api_key:
        req.add_header("OPEN-SANDBOX-API-KEY", cfg.api_key)
    try:
        with urlopen(req, timeout=timeout or cfg.request_timeout) as r:
            raw = r.read() or b"{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, OSError):
        return -1, {}


class OpenSandboxRuntime:
    """ContainerRuntime Protocol implementation backed by OpenSandbox."""

    # OpenSandbox state → mini_cc runtime status string.
    # Terminated/Failed both map to "missing" so callers can treat the
    # sandbox as gone (re-ensure_running will create a fresh one).
    _STATE_MAP = {
        "Pending": "pending",
        "Running": "running",
        "Paused": "paused",
        "Pausing": "paused",
        "Resuming": "running",
        "Stopping": "exited",
        "Terminated": "missing",
        "Failed": "missing",
    }

    def __init__(self, cfg: OpenSandboxConfig):
        self.cfg = cfg

    def is_available(self) -> bool:
        status, _ = _request(self.cfg, "GET", "/health", timeout=5)
        return status == 200

    def status(self, name: str) -> str:
        """Return mini_cc-style status string for the tid's sandbox.

        Maps OpenSandbox lifecycle states onto the smaller vocabulary used
        by DockerRuntime ('running' / 'paused' / 'missing' / etc.) so
        callers (CLI status, ContainerSandbox) work unchanged. The list
        response already carries status, so no follow-up GET."""
        sb = _find_by_tid(self.cfg, name)
        if sb is None:
            return "missing"
        state = sb.get("status", {}).get("state", "Pending")
        return self._STATE_MAP.get(state, "missing")

    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> "subprocess.CompletedProcess":
        """Run ``command`` inside the tid's sandbox; return CompletedProcess.

        Three-step: resolve sandbox by tid → resolve execd endpoint → POST
        /command/run with SSE, accumulate stdout/stderr/exit_code."""
        import subprocess
        sb = _find_by_tid(self.cfg, name)
        if sb is None:
            from .runtime import RuntimeUnavailable
            raise RuntimeUnavailable(
                f"no sandbox for tid={name}; call ensure_running first")
        sid = sb["id"]
        url, hdrs = _get_execd_endpoint(self.cfg, sid)
        payload = json.dumps({
            "command": command,
            "working_directory": workdir or None,
            "env": env or {},
        }).encode()
        raw = _stream_sse(url, hdrs, payload, timeout=timeout)
        rc, out, err = _parse_sse_output(raw)
        return subprocess.CompletedProcess(
            args=["opensandbox", "exec", name, command],
            returncode=rc, stdout=out, stderr=err)

    def stop(self, name: str) -> None:
        """Idempotent stop: DELETE the tid's sandbox if it exists."""
        sb = _find_by_tid(self.cfg, name)
        if sb is not None:
            _request(self.cfg, "DELETE", f"/sandboxes/{sb['id']}")

    def remove(self, name: str) -> None:
        """Idempotent remove — same as stop in OpenSandbox (no separate
        stopped-but-not-removed state; DELETE schedules termination)."""
        sb = _find_by_tid(self.cfg, name)
        if sb is not None:
            _request(self.cfg, "DELETE", f"/sandboxes/{sb['id']}")

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        """Return tids of all sandboxes managed by mini_cc.

        ``prefix`` arg is accepted for Protocol compat with DockerRuntime
        but ignored: OpenSandbox sandboxes are filtered by the
        ``managed-by=mini-cc`` metadata field, not by container-name prefix.
        """
        qs = "?metadata=managed-by%3Dmini-cc&pageSize=200"
        status, body = _request(self.cfg, "GET", f"/sandboxes{qs}")
        if status != 200:
            return []
        return [it.get("metadata", {}).get(_TID_KEY, "")
                for it in body.get("items", [])
                if it.get("metadata", {}).get(_TID_KEY)]

    def ensure_running(self, *, name: str, image: str,
                       mounts: list, network: str,
                       cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None:
        """Idempotent: reuse existing Running sandbox by tid, else create.

        ``name`` here is the tenant id (TenantContainerManager passes tid
        directly — see Phase 2 refactor). Mounts may be legacy tuples
        (host, container, options) or MountSpec; Phase 1 only supports host
        bind mounts since OpenSandbox Docker runtime mirrors that semantic.
        """
        tid = name
        existing = _find_by_tid(self.cfg, tid)
        if existing is not None:
            return  # already created for this tid
        body = {
            "image": {"uri": image},
            "entrypoint": ["tail", "-f", "/dev/null"],  # long-lived placeholder
            "metadata": {_TID_KEY: tid, "managed-by": "mini-cc"},
            "resourceLimits": {
                "cpu": cpu_quota or "1000m",
                "memory": memory_limit or "1Gi",
            },
            # Manual cleanup mode: no TTL, mini_cc owns lifecycle.
            # Note: only Docker runtime supports timeout=null. K8s providers
            # may reject this — see plan's Lifecycle Alignment section.
            "timeout": None,
            "volumes": _build_volumes(mounts),
        }
        if network in ("none", "disabled"):
            body["networkPolicy"] = {"defaultAction": "deny", "egress": []}
        status, resp = _request(self.cfg, "POST", "/sandboxes", body=body)
        if status not in (200, 202):
            from .runtime import RuntimeUnavailable
            raise RuntimeUnavailable(
                f"create sandbox failed: HTTP {status} {resp}")
        sid = resp.get("id")
        if not sid:
            from .runtime import RuntimeUnavailable
            raise RuntimeUnavailable(f"create returned no id: {resp}")
        _wait_running(self.cfg, sid)


# ── helpers ───────────────────────────────────────────────────────────────

_TID_KEY = "mini-cc-tid"  # DNS-label compliant (no underscore)


def _find_by_tid(cfg: OpenSandboxConfig, tid: str) -> dict | None:
    """GET /sandboxes filtered by metadata → first matching sandbox dict.

    Returns the full sandbox object (id + status + metadata) so callers
    can read state without a follow-up GET."""
    qs = f"?metadata={_TID_KEY}%3D{tid}&pageSize=1"
    status, body = _request(cfg, "GET", f"/sandboxes{qs}")
    if status != 200:
        return None
    items = body.get("items") or []
    return items[0] if items else None


def _wait_running(cfg: OpenSandboxConfig, sid: str) -> None:
    """Poll GET /sandboxes/{sid} until state=Running or timeout."""
    deadline = time.monotonic() + cfg.ready_timeout
    while time.monotonic() < deadline:
        status, body = _request(cfg, "GET", f"/sandboxes/{sid}")
        if status == 200:
            state = body.get("status", {}).get("state", "Pending")
            if state == "Running":
                return
            if state in ("Failed", "Terminated"):
                raise RuntimeError(
                    f"sandbox {sid} entered terminal state {state}")
        time.sleep(cfg.poll_interval)
    raise RuntimeError(
        f"sandbox {sid} never became Running within {cfg.ready_timeout}s")


def _build_volumes(mounts: list) -> list:
    """Translate mini_cc mount tuples/MountSpecs → OpenSandbox Volume list.

    Phase 1 only supports HostMount; Phase 2 will add PVC/OSSFS via
    MountSpec dispatch."""
    try:
        from .config import MountSpec
    except ImportError:
        MountSpec = None  # Phase 1: not yet defined
    vols = []
    for i, m in enumerate(mounts):
        if MountSpec is not None and isinstance(m, MountSpec):
            vols.append({
                "name": m.name,
                "mountPath": m.mount_path,
                "host": {"path": m.backend.path},
            })
        else:  # legacy tuple (host, container, options)
            host, container, _opts = m
            vols.append({
                "name": f"mnt-{i}",
                "mountPath": container,
                "host": {"path": host},
            })
    return vols


def _get_execd_endpoint(cfg: OpenSandboxConfig, sid: str) -> tuple[str, dict]:
    """Resolve execd's public URL + auth headers via the lifecycle server.

    Mirrors SDK behaviour (adapters/command_adapter.py:144): GET
    /sandboxes/{id}/endpoints/{execd_port} returns the endpoint URL and
    optional auth headers to forward on every execd call. URL host is
    protocol-relative in the spec; we prepend ``http://`` if missing."""
    status, body = _request(
        cfg, "GET", f"/sandboxes/{sid}/endpoints/{cfg.execd_port}")
    if status != 200:
        from .runtime import RuntimeUnavailable
        raise RuntimeUnavailable(
            f"cannot resolve execd endpoint for {sid}: HTTP {status}")
    host = body["endpoint"]
    base = host if "://" in host else f"http://{host}"
    return f"{base}/command/run", body.get("headers") or {}


def _stream_sse(url: str, headers: dict, body: bytes, timeout: int) -> bytes:
    """POST to ``url`` (execd /command/run) with SSE response; return raw
    bytes so the caller can split events. urllib blocks until close, which
    is fine — execd closes the stream on command completion."""
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_sse_output(raw: bytes) -> tuple[int, str, str]:
    """Walk an SSE byte stream, accumulate stdout/stderr text + exit code.

    Each event is two lines: ``event: <type>`` and ``data: <json>``. We
    care about ``stdout`` / ``stderr`` (carry ``text``) and ``complete``
    (carries ``exit_code``). Anything else is ignored."""
    stdout, stderr = [], []
    exit_code = 0
    for raw_evt in raw.split(b"\n\n"):
        event_type = None
        data = b""
        for line in raw_evt.split(b"\n"):
            if line.startswith(b"event:"):
                event_type = line[6:].strip().decode()
            elif line.startswith(b"data:"):
                data = line[5:].strip()
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event_type == "stdout":
            stdout.append(payload.get("text", ""))
        elif event_type == "stderr":
            stderr.append(payload.get("text", ""))
        elif event_type == "complete":
            exit_code = int(payload.get("exit_code", 0))
    return exit_code, "".join(stdout), "".join(stderr)
