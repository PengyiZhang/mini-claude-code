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
