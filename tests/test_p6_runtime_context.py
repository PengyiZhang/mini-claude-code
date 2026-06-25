"""ServerRuntimeContext: three-tier backend selection (opensandbox → docker → None).

Each test pins env + probes so the assertions stay platform-independent.
ServerRuntimeContext is built via __new__ to bypass __post_init__ so we
can call _build_runtime in isolation."""
from __future__ import annotations

import os

from mini_cc.server.runtime_context import ServerRuntimeContext


def _bare_ctx():
    """Construct ServerRuntimeContext without running __post_init__."""
    return ServerRuntimeContext.__new__(ServerRuntimeContext)


def test_build_runtime_selects_opensandbox_when_configured(monkeypatch):
    """env OPEN_SANDBOX_* set + server reachable → OpenSandboxRuntime."""
    from mini_cc.sandbox.opensandbox_runtime import OpenSandboxRuntime
    monkeypatch.setenv("MINI_CC_SANDBOX_BACKEND", "opensandbox")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "osb:8080")
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "sk")
    monkeypatch.setattr(
        "mini_cc.sandbox.opensandbox_runtime.OpenSandboxRuntime.is_available",
        lambda self: True)
    rt = ServerRuntimeContext._build_runtime(_bare_ctx())
    assert isinstance(rt, OpenSandboxRuntime)


def test_build_runtime_falls_back_when_opensandbox_unreachable(monkeypatch):
    """Explicitly opensandbox but server unreachable → fall through to docker."""
    from mini_cc.sandbox.runtime import DockerRuntime
    monkeypatch.setenv("MINI_CC_SANDBOX_BACKEND", "opensandbox")
    monkeypatch.setenv("OPEN_SANDBOX_DOMAIN", "osb:8080")
    monkeypatch.setattr(
        "mini_cc.sandbox.opensandbox_runtime.OpenSandboxRuntime.is_available",
        lambda self: False)
    monkeypatch.setattr("mini_cc.sandbox.osdetect.probe_docker",
        lambda **kw: type("A", (), {"available": True, "argv_prefix": ()})())
    rt = ServerRuntimeContext._build_runtime(_bare_ctx())
    assert isinstance(rt, DockerRuntime)


def test_build_runtime_auto_skips_opensandbox_when_no_env(monkeypatch):
    """Auto mode without OPEN_SANDBOX_* env → don't even probe, go straight to docker."""
    from mini_cc.sandbox.runtime import DockerRuntime
    monkeypatch.delenv("MINI_CC_SANDBOX_BACKEND", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    probed = []
    monkeypatch.setattr(
        "mini_cc.sandbox.opensandbox_runtime.OpenSandboxRuntime.is_available",
        lambda self: probed.append(1) or False)
    monkeypatch.setattr("mini_cc.sandbox.osdetect.probe_docker",
        lambda **kw: type("A", (), {"available": True, "argv_prefix": ()})())
    rt = ServerRuntimeContext._build_runtime(_bare_ctx())
    assert isinstance(rt, DockerRuntime)
    assert probed == []  # never even tried


def test_build_runtime_returns_none_when_nothing_available(monkeypatch):
    """Neither opensandbox nor docker → None → caller degrades to subprocess."""
    monkeypatch.setenv("MINI_CC_SANDBOX_BACKEND", "auto")
    monkeypatch.delenv("OPEN_SANDBOX_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_SANDBOX_API_KEY", raising=False)
    monkeypatch.setattr("mini_cc.sandbox.osdetect.probe_docker",
        lambda **kw: type("A", (), {"available": False, "argv_prefix": ()})())
    rt = ServerRuntimeContext._build_runtime(_bare_ctx())
    assert rt is None
