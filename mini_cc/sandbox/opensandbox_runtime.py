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

    def __init__(self, cfg: OpenSandboxConfig):
        self.cfg = cfg

    def is_available(self) -> bool:
        status, _ = _request(self.cfg, "GET", "/health", timeout=5)
        return status == 200

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
        sid = _find_by_tid(self.cfg, tid)
        if sid is not None:
            return
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


def _find_by_tid(cfg: OpenSandboxConfig, tid: str) -> str | None:
    """GET /sandboxes filtered by metadata → first matching id, or None."""
    qs = f"?metadata={_TID_KEY}%3D{tid}&pageSize=1"
    status, body = _request(cfg, "GET", f"/sandboxes{qs}")
    if status != 200:
        return None
    items = body.get("items") or []
    return items[0]["id"] if items else None


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
