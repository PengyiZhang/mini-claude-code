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

    @property
    def root_url(self) -> str:
        """``base_url`` without the ``/v1`` suffix — for ``/health`` etc.

        OpenSandbox's health endpoint lives at the server root, not under
        ``/v1``. Strip our own suffix rather than re-deriving from env so
        callers who construct a literal ``base_url`` still work."""
        return self.base_url[:-3] if self.base_url.endswith("/v1") \
            else self.base_url

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
    can treat "server unreachable" the same as "not available".

    ``path`` may be a relative path (joined onto ``cfg.base_url``) or an
    absolute URL (used as-is) — needed for ``/health`` which lives at the
    server root, not under ``/v1``."""
    url = path if path.startswith("http") else f"{cfg.base_url}{path}"
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
        # /health lives at the server root, not under /v1.
        status, _ = _request(self.cfg, "GET",
                             f"{self.cfg.root_url}/health", timeout=5)
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
        /command with a JSON-per-line stream response, accumulate
        stdout/stderr/exit_code."""
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
            "cwd": workdir or None,
            "envs": env or {},
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

    def interp_for(self, tid: str):
        """Build an OpenSandboxInterpreter bound to tid's sandbox.

        Used by the ``execute_code`` tool when MINI_CC_REPL_BACKEND=
        opensandbox. Resolves the sandbox + execd endpoint lazily so
        ensure_running has a chance to have run first. Raises
        RuntimeUnavailable if no sandbox exists for this tid."""
        from ..tools.opensandbox_interp import OpenSandboxInterpreter
        sb = _find_by_tid(self.cfg, tid)
        if sb is None:
            from .runtime import RuntimeUnavailable
            raise RuntimeUnavailable(
                f"no sandbox for tid={tid}; ensure_running first")
        sid = sb["id"]
        url, hdrs = _get_execd_endpoint(self.cfg, sid)
        return OpenSandboxInterpreter(sid, url, hdrs)

    def build_image(self, tag: str, context_dir, dockerfile=None) -> None:
        """OpenSandbox consumes pre-built images, so building stays local.

        Delegates to DockerRuntime so the existing imagebuild pipeline
        (Dockerfile render + docker build) keeps working. This is what
        ``cmd_sandbox_build_image`` and ServerRuntimeContext call when they
        need to (re)build the sandbox image."""
        from .runtime import DockerRuntime
        DockerRuntime().build_image(tag, context_dir, dockerfile)

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


def _mount_spec_to_volume(m: "MountSpec") -> dict:
    """Translate one MountSpec → OpenSandbox Volume dict.

    OpenSandbox's Volume shape is a discriminated union on the backend
    key (``host`` / ``pvc`` / ``ossfs``). ``readOnly`` and ``subPath``
    apply to any backend. Storage fields are only included when set so
    the server applies its own defaults rather than receiving an empty
    string."""
    from .config import HostMount, OSSFSMount, PVCMount
    v: dict = {"name": m.name, "mountPath": m.mount_path}
    if isinstance(m.backend, HostMount):
        v["host"] = {"path": m.backend.path}
    elif isinstance(m.backend, PVCMount):
        v["pvc"] = {
            "claimName": m.backend.claim_name,
            "createIfNotExists": m.backend.create_if_not_exists,
        }
        if m.backend.storage_class:
            v["pvc"]["storageClass"] = m.backend.storage_class
        if m.backend.storage:
            v["pvc"]["storage"] = m.backend.storage
    elif isinstance(m.backend, OSSFSMount):
        v["ossfs"] = {
            "bucket": m.backend.bucket,
            "endpoint": m.backend.endpoint,
            "accessKeyId": m.backend.access_key_id,
            "accessKeySecret": m.backend.access_key_secret,
        }
    else:  # pragma: no cover — MountBackend union is closed
        raise TypeError(f"unsupported mount backend: {type(m.backend).__name__}")
    if m.read_only:
        v["readOnly"] = True
    if m.sub_path:
        v["subPath"] = m.sub_path
    return v


def _build_volumes(mounts: list) -> list:
    """Translate mini_cc mount tuples/MountSpecs → OpenSandbox Volume list.

    Accepts legacy tuples ``(host, container, options)`` for callers that
    haven't been updated yet — they're converted via
    ``MountSpec.from_legacy_tuple`` so the unified translation path runs
    for both shapes. Tuples get a synthesized name (``mnt-<idx>``) to
    keep them stable across re-creates."""
    from .config import MountSpec
    vols = []
    for i, m in enumerate(mounts):
        if isinstance(m, MountSpec):
            vols.append(_mount_spec_to_volume(m))
        else:
            spec = MountSpec.from_legacy_tuple(m)
            # Override the name to match Phase 1's positional naming so
            # existing fixtures don't break.
            spec = MountSpec(
                name=f"mnt-{i}", mount_path=spec.mount_path,
                backend=spec.backend, read_only=spec.read_only,
                sub_path=spec.sub_path)
            vols.append(_mount_spec_to_volume(spec))
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
    return f"{base}/command", body.get("headers") or {}


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
    """Walk execd's response stream, accumulate stdout/stderr + exit code.

    Despite the ``text/event-stream`` content type, execd emits one JSON
    object per line (NOT standard SSE ``event:``/``data:`` framing). Each
    line carries a ``type`` field: ``init`` / ``ping`` / ``stdout`` /
    ``stderr`` / ``execution_complete``. The completion event has no
    ``exit_code`` field — non-zero exits surface as a stderr line, so we
    default to 0 on completion. (If a future spec adds ``exit_code``, we
    pick it up via ``.get("exit_code", 0)``.)"""
    stdout, stderr = [], []
    exit_code = 0
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = payload.get("type")
        if etype == "stdout":
            stdout.append(payload.get("text", ""))
        elif etype == "stderr":
            stderr.append(payload.get("text", ""))
        elif etype == "execution_complete":
            exit_code = int(payload.get("exit_code", 0))
    return exit_code, "".join(stdout), "".join(stderr)
