"""OpenSandboxRuntime: ContainerRuntime Protocol 的远端 HTTP 实现。

后端是 OpenSandbox lifecycle server (FastAPI :8080 之类) + 容器内 execd
agent (:44772)。所有调用走 stdlib urllib，不依赖 opensandbox SDK —— 与
mini_cc/mcp/http.py 风格一致，避免拖入 mcp/pydantic/async machinery 重依赖。

幂等策略：每个 mini_cc 租户对应一个 OpenSandbox sandbox，通过 metadata 字段
``mini-cc-tid=<tid>`` 索引。OpenSandbox spec 要求 metadata key 符合 DNS label
规则（首尾字母数字，中段可含 ``-_.``），故用连字符不用下划线。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


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
