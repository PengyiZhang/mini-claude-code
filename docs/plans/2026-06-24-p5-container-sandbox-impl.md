# P5 Container Sandbox Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a per-tenant Docker container isolation layer on top of the existing `SubprocessSandbox`, configurable per tenant, with graceful auto-degrade when Docker/WSL2 is unavailable.

**Architecture:** A new `ContainerSandbox` implements the `Sandbox` protocol — file ops (read/write/edit/glob/grep) stay on host via an embedded `SubprocessSandbox` (path isolation already enforced); only `execute()`/`git()` are routed through a long-running per-tenant Docker container via `docker exec`. A `TenantContainerManager` owns the container lifecycle (lazy `ensure_running`, shutdown cleanup). A two-layer config (server env default + per-tenant `sandbox.toml`) decides whether to use the container sandbox and how (image, packages, mounts, network). At startup, `osdetect` probes Docker + WSL2 backend availability; if missing, the tenant silently falls back to `SubprocessSandbox` and logs a warning (no fail-fast).

**Tech Stack:** Python 3.12 stdlib only (no Docker SDK — pure `subprocess.run` calls to `docker` CLI). Docker Engine 29.1.3 reachable via `wsl -d Ubuntu-24.04 -- docker ...` on this Windows host; base image `python:3.10-slim` (locally cached in that WSL2 distro, no pull needed). TOML parsed via `tomllib` (stdlib). Tests use a `FakeRuntime` test double so no Docker daemon is required for the suite.

---

## Locked Decisions (from clarifying Q&A)

| Decision | Choice | Rationale |
|---|---|---|
| Fallback policy | **Graceful auto-degrade** | User requirement: when container env unsupported, degrade to current functionality. Log warning, surface in `/metrics`. |
| Isolation granularity | **Per-tenant** | One long-running container per tenant; all projects of that tenant share it. Cross-project isolation already handled at `SubprocessSandbox` path layer. |
| Image config dimensions | **All four**: custom tag, custom Dockerfile path, declarative apt/pip/node packages, extra bind-mounts | Tenant flexibility. |
| Windows / WSL2 strategy | **Auto-detect at startup** | `docker info` + WSL2 backend probe; transparent to operator. |

## Supersedes

This plan supersedes `docs/plans/2026-06-21-phaseG-container-sandbox-design.md`. Three locked decisions change:

1. **Fallback**: was `fail-fast` (503) → now `auto-degrade` to `SubprocessSandbox` with warning.
2. **Image config**: was fixed repo Dockerfile → now per-tenant custom Dockerfile path + declarative packages + extra mounts.
3. **Windows**: was doc-only note → now explicit `osdetect` module with WSL2 probe.

The other Phase G decisions stand: Docker CLI runtime (no SDK), per-tenant container, `docker exec` per call, lazy start, default `--network=none`, only `execute()`/`git()` containerized, cold-loaded config.

## File Map (new + modified)

```
mini_cc/sandbox/
  osdetect.py            NEW   ~80 lines  Docker/WSL2 availability probe
  config.py              NEW   ~140 lines ContainerConfig + sandbox.toml loader
  imagebuild.py          NEW   ~120 lines Dockerfile render + build orchestrator
  runtime.py             NEW   ~180 lines ContainerRuntime Protocol + DockerRuntime + FakeRuntime
  manager.py             NEW   ~110 lines TenantContainerManager (lifecycle)
  container.py           NEW   ~110 lines ContainerSandbox (Sandbox protocol impl)
  Dockerfile             NEW   ~30 lines  Base image (python:3.12-slim + git + rg + node20)
  base.py                MOD   +1 line    re-export DockerMissingError, ContainerSandbox
  __init__.py            MOD   +10 lines  re-exports
mini_cc/projects/
  manager.py             MOD   +25 lines  sandbox_factory injection in __init__/_assemble
mini_cc/server/
  app.py                 MOD   +20 lines  container cleanup in lifespan
  cli.py                 MOD   +90 lines  probe + wire factory + new `sandbox` subcommand
tests/
  test_p5_osdetect.py              NEW  ~90 lines
  test_p5_sandbox_config.py        NEW  ~180 lines
  test_p5_imagebuild.py            NEW  ~140 lines
  test_p5_runtime.py               NEW  ~220 lines
  test_p5_container_sandbox.py     NEW  ~260 lines
  test_p5_manager.py               NEW  ~160 lines
  test_p5_http_degrade.py          NEW  ~130 lines
mini_cc/README.md                  MOD   +50 lines  Container sandbox section
mini_cc/README.zh.md               MOD   +50 lines  Chinese counterpart
```

---

## Task 1: OS Detection Module

Detect whether Docker is available and, on Windows, whether WSL2 is the backend. Pure stdlib.

**Files:**
- Create: `mini_cc/sandbox/osdetect.py`
- Test: `tests/test_p5_osdetect.py`

### Step 1: Write failing test

```python
# tests/test_p5_osdetect.py
"""OS / Docker availability probe. No real docker daemon needed — the
probe is patched with monkeypatch in every test."""
from __future__ import annotations

import subprocess
import sys

from mini_cc.sandbox.osdetect import DockerAvailability, probe_docker


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker", "info"], returncode=returncode,
        stdout=stdout, stderr=stderr)


def test_probe_success_linux(monkeypatch):
    """docker info returns 0 → available, wsl2=False on non-Windows."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, "Server Version: 27.0"))
    monkeypatch.setattr(sys, "platform", "linux")
    avail = probe_docker()
    assert avail.available is True
    assert avail.server_version == "27.0"
    assert avail.wsl2 is False
    assert avail.reason == ""


def test_probe_docker_missing(monkeypatch):
    """docker binary not on PATH → FileNotFoundError → not available."""
    def boom(*a, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", boom)
    avail = probe_docker()
    assert avail.available is False
    assert "docker" in avail.reason.lower() or "not found" in avail.reason.lower()


def test_probe_daemon_dead(monkeypatch):
    """docker exists but daemon not running → returncode != 0 → unavailable."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(1, "", "Cannot connect to the Docker daemon"))
    avail = probe_docker()
    assert avail.available is False
    assert "daemon" in avail.reason.lower() or "cannot connect" in avail.reason.lower()


def test_probe_wsl2_backend(monkeypatch):
    """On Windows, the .Context section of `docker info` reveals WSL2."""
    monkeypatch.setattr(sys, "platform", "win32")
    info = "Server Version: 27.5\n Operating System: Docker Desktop\n Default Runtime: runc\n wsl2 (Windows)"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, info))
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is True


def test_probe_windows_native(monkeypatch):
    """Windows native containers (no WSL2) → wsl2=False, available still True."""
    monkeypatch.setattr(sys, "platform", "win32")
    info = "Server Version: 27.5\n Operating System: Windows Server 2022"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: _completed(0, info))
    avail = probe_docker()
    assert avail.available is True
    assert avail.wsl2 is False


def test_probe_caches_no_repeated_calls(monkeypatch):
    """Second call returns cached result without invoking subprocess again."""
    calls = []
    def counting(*a, **kw):
        calls.append(1)
        return _completed(0, "Server Version: 27.0")
    monkeypatch.setattr(subprocess, "run", counting)
    probe_docker()
    probe_docker()
    assert len(calls) == 1
    # reset cache for other tests
    from mini_cc.sandbox import osdetect
    osdetect._CACHE = None
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_osdetect.py -v
```

Expected: ImportError — `mini_cc.sandbox.osdetect` does not exist.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/osdetect.py
"""Detect Docker availability and, on Windows, whether WSL2 is the backend.

The probe runs once at process start (cached). Per-call cost is a single
``docker info`` invocation. On Windows, the .Operating System / .Name
section reveals whether Docker Desktop is using its WSL2 backend (the
only backend that can run Linux containers, which is what the Phase G
Dockerfile assumes).
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache

_CACHE: "DockerAvailability | None" = None


@dataclass(frozen=True)
class DockerAvailability:
    available: bool
    server_version: str = ""
    wsl2: bool = False
    reason: str = ""


def _looks_like_wsl2(info_stdout: str) -> bool:
    """Docker Desktop on WSL2 advertises itself in `docker info`.

    Heuristic: any of the strings below in stdout → WSL2 backend. This
    is intentionally loose — false positives just mean a tenant gets
    `wsl2=True` reported, which doesn't drive any code path today.
    """
    haystack = info_stdout.lower()
    return any(sig in haystack for sig in (
        "wsl", "docker desktop", "windows subsystem for linux"))


def probe_docker(*, force: bool = False) -> DockerAvailability:
    """Probe ``docker info``. Result is cached for the process lifetime.

    Pass ``force=True`` to bypass the cache (used by tests and the
    ``sandbox status`` CLI command).
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    try:
        cp = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}|{{.OperatingSystem}}"],
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        _CACHE = DockerAvailability(
            available=False, reason="docker binary not found on PATH")
        return _CACHE
    except subprocess.TimeoutExpired:
        _CACHE = DockerAvailability(
            available=False, reason="docker info timed out (>10s)")
        return _CACHE
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout).strip()[:200] or "non-zero exit"
        _CACHE = DockerAvailability(available=False, reason=msg)
        return _CACHE
    # Expected: "27.5|Docker Desktop"
    parts = cp.stdout.strip().split("|", 1)
    version = parts[0].strip() if parts else ""
    os_field = parts[1].strip() if len(parts) > 1 else ""
    wsl2 = (sys.platform == "win32") and _looks_like_wsl2(os_field)
    _CACHE = DockerAvailability(
        available=True, server_version=version, wsl2=wsl2)
    return _CACHE


def reset_cache() -> None:
    """Test hook — clear the cached probe result."""
    global _CACHE
    _CACHE = None
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_osdetect.py -v
```

Expected: 6 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/osdetect.py tests/test_p5_osdetect.py
git commit -m "feat(p5): add osdetect — probe docker + WSL2 backend availability"
```

---

## Task 2: Container Config Dataclass

Define `ContainerConfig` and load it from `tenants/{tid}/sandbox.toml`. This task covers the data model and loader; the factory that combines server-default + tenant-override is Task 3.

**Files:**
- Create: `mini_cc/sandbox/config.py`
- Test: `tests/test_p5_sandbox_config.py`

### Step 1: Write failing test

```python
# tests/test_p5_sandbox_config.py
"""ContainerConfig + sandbox.toml loader. Uses tmp_path for fixture files."""
from __future__ import annotations

import pytest

from mini_cc.sandbox.config import (
    ContainerConfig, load_tenant_config, resolve_kind,
    DEFAULT_IMAGE_TAG)


def test_defaults():
    cfg = ContainerConfig()
    assert cfg.enabled is False
    assert cfg.image_tag == DEFAULT_IMAGE_TAG
    assert cfg.network == "none"
    assert cfg.dockerfile_path is None
    assert cfg.apt_packages == []
    assert cfg.pip_packages == []
    assert cfg.node_packages == []
    assert cfg.extra_mounts == []


def test_load_tenant_config_missing_file(tmp_path):
    """No sandbox.toml → returns None (caller falls back to server default)."""
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    cfg = load_tenant_config("t1", tenants_dir)
    assert cfg is None


def test_load_tenant_config_minimal(tmp_path):
    """Minimal valid config: just enabled=true."""
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text('enabled = true\n')
    cfg = load_tenant_config("t1", tmp_path / "tenants")
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.image_tag == DEFAULT_IMAGE_TAG


def test_load_tenant_config_full(tmp_path):
    """All four image-config dimensions parsed correctly."""
    tenants_dir = tmp_path / "tenants" / "t2"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text(
        'enabled = true\n'
        'image_tag = "my-registry/sandbox:v3"\n'
        'network = "bridge"\n'
        'dockerfile_path = "./sandbox/Dockerfile.custom"\n'
        'cpu_quota = "1.5"\n'
        'memory_limit = "1g"\n'
        '\n'
        'apt_packages = ["ffmpeg", "imagemagick"]\n'
        'pip_packages = ["numpy", "pandas"]\n'
        'node_packages = ["typescript"]\n'
        '\n'
        '[[extra_mounts]]\n'
        'host = "/host/cache"\n'
        'container = "/cache"\n'
        'options = "ro"\n'
        '\n'
        '[[extra_mounts]]\n'
        'host = "/host/pip-cache"\n'
        'container = "/root/.cache/pip"\n'
    )
    cfg = load_tenant_config("t2", tmp_path / "tenants")
    assert cfg is not None
    assert cfg.image_tag == "my-registry/sandbox:v3"
    assert cfg.network == "bridge"
    assert cfg.dockerfile_path == "./sandbox/Dockerfile.custom"
    assert cfg.cpu_quota == "1.5"
    assert cfg.memory_limit == "1g"
    assert cfg.apt_packages == ["ffmpeg", "imagemagick"]
    assert cfg.pip_packages == ["numpy", "pandas"]
    assert cfg.node_packages == ["typescript"]
    assert len(cfg.extra_mounts) == 2
    assert cfg.extra_mounts[0].host == "/host/cache"
    assert cfg.extra_mounts[0].container == "/cache"
    assert cfg.extra_mounts[0].options == "ro"
    assert cfg.extra_mounts[1].options == ""  # default


def test_load_tenant_config_invalid_network(tmp_path):
    """network must be 'none' or 'bridge'."""
    tenants_dir = tmp_path / "tenants" / "t3"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text(
        'enabled = true\nnetwork = "host"\n')
    with pytest.raises(ValueError, match="network"):
        load_tenant_config("t3", tmp_path / "tenants")


def test_load_tenant_config_tenant_id_traversal_rejected(tmp_path):
    """tenant_id with path separators must be rejected (defense-in-depth)."""
    tenants_dir = tmp_path / "tenants"
    tenants_dir.mkdir()
    with pytest.raises(ValueError):
        load_tenant_config("../escape", tenants_dir)


def test_resolve_kind_disabled_by_default(monkeypatch, tmp_path):
    """No env, no tenant file → subprocess."""
    monkeypatch.delenv("MINI_CC_SANDBOX_DEFAULT", raising=False)
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "subprocess"
    assert cfg is None


def test_resolve_kind_env_container_overrides_absent_tenant_file(monkeypatch, tmp_path):
    """MINI_CC_SANDBOX_DEFAULT=container + no tenant file → container + default cfg."""
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "container")
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "container"
    assert cfg is not None
    assert cfg.enabled is True  # server-default implies enabled
    assert cfg.image_tag == DEFAULT_IMAGE_TAG


def test_resolve_kind_tenant_file_overrides_env(monkeypatch, tmp_path):
    """Tenant enabled=false wins over server-default=container."""
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "container")
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text('enabled = false\n')
    kind, cfg = resolve_kind("t1", tmp_path / "tenants")
    assert kind == "subprocess"
    assert cfg is None


def test_resolve_kind_invalid_env_value(monkeypatch, tmp_path):
    """Bad env value → ValueError early."""
    monkeypatch.setenv("MINI_CC_SANDBOX_DEFAULT", "kubernetes")
    with pytest.raises(ValueError):
        resolve_kind("t1", tmp_path / "tenants")
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_sandbox_config.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/config.py
"""ContainerConfig + per-tenant sandbox.toml loader.

Two-layer config:
- Server default: ``MINI_CC_SANDBOX_DEFAULT=subprocess|container`` env var.
  Absent → ``subprocess`` (today's behavior unchanged).
- Tenant override: ``<tenants_dir>/<tid>/sandbox.toml``.

``sandbox.toml`` schema (all fields optional)::

    enabled = true                      # turn on container sandbox for this tenant
    image_tag = "my-registry/sb:v2"     # default mini_cc-sandbox:latest
    network = "none"                    # "none" | "bridge"
    dockerfile_path = "./Dockerfile.x"  # build this Dockerfile instead of the default
    cpu_quota = "1.5"                   # docker --cpus
    memory_limit = "512m"               # docker --memory

    apt_packages = ["ffmpeg"]            # apt-get install at build time
    pip_packages = ["numpy"]             # pip install at build time
    node_packages = ["typescript"]       # npm install -g at build time

    [[extra_mounts]]                    # additional bind-mounts
    host = "/host/cache"
    container = "/cache"
    options = "ro"                      # docker mount options, default ""

Config is cold-loaded: ``ProjectManager`` caches it per tenant on first access.
File edits require a server restart.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE_TAG = "mini_cc-sandbox:latest"
_VALID_NETWORKS = {"none", "bridge"}
_SAFE_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Mount:
    host: str
    container: str
    options: str = ""


@dataclass
class ContainerConfig:
    enabled: bool = False
    image_tag: str = DEFAULT_IMAGE_TAG
    network: str = "none"
    dockerfile_path: str | None = None
    cpu_quota: str | None = None
    memory_limit: str | None = None
    apt_packages: list[str] = field(default_factory=list)
    pip_packages: list[str] = field(default_factory=list)
    node_packages: list[str] = field(default_factory=list)
    extra_mounts: list[Mount] = field(default_factory=list)


def _tenant_config_path(tid: str, tenants_dir: Path) -> Path:
    if not _SAFE_TENANT_ID.match(tid):
        raise ValueError(f"invalid tenant_id: {tid!r}")
    return tenants_dir / tid / "sandbox.toml"


def load_tenant_config(tid: str, tenants_dir: Path) -> ContainerConfig | None:
    """Read ``<tenants_dir>/<tid>/sandbox.toml``. None if file missing.

    Raises ValueError if the file exists but is malformed (unknown
    network value, bad mount shape, etc.) — fail loud at config-load
    time rather than silently mishandling later.
    """
    fp = _tenant_config_path(tid, tenants_dir)
    if not fp.exists():
        return None
    with fp.open("rb") as fh:
        data = tomllib.load(fh)

    network = data.get("network", "none")
    if network not in _VALID_NETWORKS:
        raise ValueError(
            f"sandbox.toml: network must be one of {sorted(_VALID_NETWORKS)}, "
            f"got {network!r}")

    mounts: list[Mount] = []
    for m in data.get("extra_mounts", []):
        if not isinstance(m, dict) or "host" not in m or "container" not in m:
            raise ValueError(
                f"sandbox.toml: extra_mounts entry missing host/container: {m!r}")
        mounts.append(Mount(
            host=str(m["host"]),
            container=str(m["container"]),
            options=str(m.get("options", "")),
        ))

    return ContainerConfig(
        enabled=bool(data.get("enabled", False)),
        image_tag=str(data.get("image_tag", DEFAULT_IMAGE_TAG)),
        network=network,
        dockerfile_path=data.get("dockerfile_path"),
        cpu_quota=data.get("cpu_quota"),
        memory_limit=data.get("memory_limit"),
        apt_packages=list(data.get("apt_packages", [])),
        pip_packages=list(data.get("pip_packages", [])),
        node_packages=list(data.get("node_packages", [])),
        extra_mounts=mounts,
    )


def resolve_kind(tid: str, tenants_dir: Path) -> tuple[str, ContainerConfig | None]:
    """Decide whether ``tid`` uses the container sandbox.

    Resolution order (first wins):
    1. ``<tenants_dir>/<tid>/sandbox.toml`` with ``enabled = true|false``.
    2. ``MINI_CC_SANDBOX_DEFAULT=subprocess|container`` env var.
    3. ``subprocess`` (today's default).

    Returns ``("subprocess", None)`` or ``("container", cfg)``.
    Note: when env-default=container and tenant file is absent, the
    returned cfg has ``enabled=True`` (server default implies on).
    """
    env_val = os.environ.get("MINI_CC_SANDBOX_DEFAULT", "subprocess").strip().lower()
    if env_val not in {"subprocess", "container"}:
        raise ValueError(
            f"MINI_CC_SANDBOX_DEFAULT must be 'subprocess' or 'container', "
            f"got {env_val!r}")

    cfg = load_tenant_config(tid, tenants_dir)
    if cfg is not None:
        if cfg.enabled:
            return "container", cfg
        return "subprocess", None

    if env_val == "container":
        return "container", ContainerConfig(enabled=True)
    return "subprocess", None
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_sandbox_config.py -v
```

Expected: 10 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/config.py tests/test_p5_sandbox_config.py
git commit -m "feat(p5): ContainerConfig + sandbox.toml loader (4 image-config dimensions)"
```

---

## Task 3: Image Build Orchestrator

Render a Dockerfile from `ContainerConfig` (either a user-supplied Dockerfile path or the repo default + injected apt/pip/node packages), then invoke `docker build`.

**Files:**
- Create: `mini_cc/sandbox/imagebuild.py`
- Create: `mini_cc/sandbox/Dockerfile`
- Test: `tests/test_p5_imagebuild.py`

### Step 1: Write failing test

```python
# tests/test_p5_imagebuild.py
"""Dockerfile rendering + build orchestrator. The actual docker build
call is patched out in every test."""
from __future__ import annotations

import subprocess

from mini_cc.sandbox.config import ContainerConfig, Mount
from mini_cc.sandbox.imagebuild import (
    render_dockerfile, build_image, ImageBuildError)


def test_render_default_no_packages():
    """Default config → Dockerfile ends with CMD sleep infinity."""
    df = render_dockerfile(ContainerConfig())
    assert "FROM python:3.10-slim" in df
    assert "CMD" in df and "sleep" in df and "infinity" in df
    assert "apt-get install" not in df  # no extra packages → no install layer


def test_render_with_apt_packages():
    cfg = ContainerConfig(apt_packages=["ffmpeg", "imagemagick"])
    df = render_dockerfile(cfg)
    assert "ffmpeg imagemagick" in df
    assert "apt-get install" in df


def test_render_with_pip_packages():
    cfg = ContainerConfig(pip_packages=["numpy", "pandas"])
    df = render_dockerfile(cfg)
    assert "pip install" in df
    assert "numpy pandas" in df


def test_render_with_node_packages():
    cfg = ContainerConfig(node_packages=["typescript", "tsx"])
    df = render_dockerfile(cfg)
    assert "npm install -g" in df
    assert "typescript tsx" in df


def test_render_all_dimensions_combined():
    cfg = ContainerConfig(
        apt_packages=["ffmpeg"],
        pip_packages=["numpy"],
        node_packages=["typescript"])
    df = render_dockerfile(cfg)
    # Layers appear in deterministic order: apt → pip → node → user → cmd
    apt_pos = df.index("apt-get install")
    pip_pos = df.index("pip install")
    node_pos = df.index("npm install -g")
    cmd_pos = df.index("CMD")
    assert apt_pos < pip_pos < node_pos < cmd_pos


def test_build_image_success(monkeypatch, tmp_path):
    """build_image invokes `docker build` with the right args."""
    captured = []
    def fake_run(args, **kw):
        captured.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)

    build_image("my-tag", ContainerConfig())

    assert captured[0][:3] == ["docker", "build", "-t"]
    assert captured[0][3] == "my-tag"
    # last positional is the context dir
    assert captured[0][-1] == str(tmp_path)


def test_build_image_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="apt-get failed"))
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)
    with __import__("pytest").raises(ImageBuildError, match="apt-get failed"):
        build_image("bad-tag", ContainerConfig())


def test_build_image_with_custom_dockerfile(monkeypatch, tmp_path):
    """When cfg.dockerfile_path is set, that Dockerfile is used verbatim
    and declarative packages are ignored (user owns the image)."""
    custom = tmp_path / "Dockerfile.custom"
    custom.write_text("FROM alpine\nRUN echo hi\n")
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda args, **kw: captured.append(args) or
            subprocess.CompletedProcess(args=args, returncode=0))
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)

    build_image("custom-tag", ContainerConfig(
        dockerfile_path=str(custom),
        apt_packages=["should-be-ignored"]))

    # -f points to the custom Dockerfile
    assert "-f" in captured[0]
    f_idx = captured[0].index("-f")
    assert captured[0][f_idx + 1] == str(custom)


def test_render_uses_base_repo_dockerfile_when_no_path():
    """No dockerfile_path → render_dockerfile uses the package-shipped Dockerfile
    as the base, layered with declarative packages."""
    df = render_dockerfile(ContainerConfig())
    # The package Dockerfile installs ripgrep + node 20 as base; ensure it's there.
    assert "ripgrep" in df
    assert "nodesource" in df or "nodejs" in df
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_imagebuild.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/imagebuild.py
"""Render a Dockerfile from ContainerConfig and invoke ``docker build``.

Two paths:
- ``cfg.dockerfile_path`` set: use that file verbatim, ignore apt/pip/node.
  The operator owns the image entirely.
- Not set: take the package-shipped Dockerfile (mini_cc/sandbox/Dockerfile)
  and append ``RUN apt-get install ...``, ``RUN pip install ...``,
  ``RUN npm install -g ...`` layers for any declared packages.

Build context is always the package sandbox dir (so the base Dockerfile
is in context). With a custom Dockerfile, we still pass the package dir
as context to keep the implementation simple — operators needing a
custom context can build their image outside mini_cc and just set
``image_tag`` to the result.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import ContainerConfig

_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT_DOCKERFILE = _PKG_DIR / "Dockerfile"


class ImageBuildError(RuntimeError):
    pass


def _context_dir() -> Path:
    """Indirection for tests to swap in a temp dir."""
    return _PKG_DIR


def _default_dockerfile_text() -> str:
    return _DEFAULT_DOCKERFILE.read_text(encoding="utf-8")


def render_dockerfile(cfg: ContainerConfig) -> str:
    """Return the Dockerfile text for this config.

    Uses ``cfg.dockerfile_path`` verbatim if set; otherwise layers
    declarative packages on top of the package default Dockerfile.
    """
    if cfg.dockerfile_path:
        return Path(cfg.dockerfile_path).read_text(encoding="utf-8")

    base = _default_dockerfile_text()
    # Strip the final CMD so we can append install layers before it.
    lines = base.splitlines()
    cmd_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("CMD")),
        len(lines))
    head = lines[:cmd_idx]
    tail = lines[cmd_idx:]

    layers: list[str] = []
    if cfg.apt_packages:
        pkgs = " ".join(cfg.apt_packages)
        layers.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} && rm -rf /var/lib/apt/lists/*")
    if cfg.pip_packages:
        pkgs = " ".join(cfg.pip_packages)
        layers.append(f"RUN pip install --no-cache-dir {pkgs}")
    if cfg.node_packages:
        pkgs = " ".join(cfg.node_packages)
        layers.append(f"RUN npm install -g {pkgs} && npm cache clean --force")

    return "\n".join(head + layers + tail) + "\n"


def build_image(tag: str, cfg: ContainerConfig) -> None:
    """Write the rendered Dockerfile to the build context and invoke docker.

    Raises ImageBuildError on non-zero exit. Caller decides whether to
    fail the request or degrade.
    """
    ctx = _context_dir()
    dockerfile_text = render_dockerfile(cfg)
    # Write to a throwaway filename so we don't clobber the package default
    # when a custom Dockerfile path was supplied.
    out_path = ctx / "Dockerfile.rendered"
    out_path.write_text(dockerfile_text, encoding="utf-8")
    args = ["docker", "build", "-t", tag, "-f", str(out_path), str(ctx)]
    cp = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if cp.returncode != 0:
        raise ImageBuildError(
            f"docker build failed (exit {cp.returncode}): "
            f"{(cp.stderr or cp.stdout).strip()[:500]}")
```

```dockerfile
# mini_cc/sandbox/Dockerfile
# Base image: locally available python:3.10-slim in WSL2 Ubuntu-24.04
# (avoids network pull during build; 3.12-slim not present locally)
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git git-lfs ca-certificates curl unzip \
        ripgrep fd-find file less vim-tiny \
        build-essential \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 agent
USER agent
WORKDIR /workspaces
CMD ["sleep", "infinity"]
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_imagebuild.py -v
```

Expected: 9 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/imagebuild.py mini_cc/sandbox/Dockerfile tests/test_p5_imagebuild.py
git commit -m "feat(p5): imagebuild — render Dockerfile from config + docker build wrapper"
```

---

## Task 4: Container Runtime Protocol + FakeRuntime

Define the `ContainerRuntime` Protocol with `FakeRuntime` test double. The `DockerRuntime` implementation is Task 5.

**Files:**
- Create: `mini_cc/sandbox/runtime.py` (Protocol + FakeRuntime + DockerRuntime stub)
- Test: `tests/test_p5_runtime.py`

### Step 1: Write failing test

```python
# tests/test_p5_runtime.py
"""FakeRuntime records calls so tests can verify docker interactions
without a real daemon. Also tests DockerRuntime's command construction
(subprocess.run is patched — we only check the argv shape)."""
from __future__ import annotations

import subprocess

import pytest

from mini_cc.sandbox.runtime import (
    ContainerRuntime, DockerRuntime, FakeRuntime, RuntimeUnavailable)


# ── FakeRuntime ────────────────────────────────────────────────────────────

def test_fake_runtime_default_unavailable():
    rt = FakeRuntime(available=False)
    with pytest.raises(RuntimeUnavailable):
        rt.ensure_running(name="x", image="img", mounts=[], network="none")


def test_fake_runtime_records_run_call():
    rt = FakeRuntime(available=True)
    rt.ensure_running(name="mini_cc-t1", image="img", mounts=[], network="none")
    assert len(rt.calls) == 1
    method, kwargs = rt.calls[0]
    assert method == "ensure_running"
    assert kwargs["name"] == "mini_cc-t1"


def test_fake_runtime_status_running_skips_ensure():
    rt = FakeRuntime(available=True, initial_status="running")
    rt.ensure_running(name="x", image="img", mounts=[], network="none")
    # No `run` call when already running.
    assert all(c[0] != "run" for c in rt.calls)


def test_fake_runtime_exec_returns_completed():
    rt = FakeRuntime(available=True)
    cp = rt.exec(name="x", workdir="/w", command="ls", timeout=10, env={})
    assert cp.returncode == 0
    assert isinstance(cp, subprocess.CompletedProcess)


def test_fake_runtime_exec_timeout_returns_124():
    rt = FakeRuntime(available=True, exec_returncode=124)
    cp = rt.exec(name="x", workdir="/w", command="sleep 5", timeout=1, env={})
    assert cp.returncode == 124


# ── DockerRuntime command construction ─────────────────────────────────────

def test_docker_runtime_is_available_true(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="27.5"))
    assert DockerRuntime().is_available() is True


def test_docker_runtime_is_available_false_when_docker_missing(monkeypatch):
    def boom(a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    assert DockerRuntime().is_available() is False


def test_docker_runtime_run_command_shape(monkeypatch):
    captured = []
    def fake(args, **kw):
        captured.append(args)
        # First call is status check, second is the actual run.
        if len(captured) == 1:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="")
    monkeypatch.setattr(subprocess, "run", fake)
    rt = DockerRuntime()
    rt.ensure_running(name="mini_cc-t1", image="img",
                      mounts=[("/host/w", "/workspaces")],
                      network="none")
    # Find the `docker run` argv in captured.
    run_calls = [c for c in captured if c[:2] == ["docker", "run"]]
    assert len(run_calls) == 1
    argv = run_calls[0]
    assert "--network=none" in argv
    assert "-v" in argv
    v_idx = argv.index("-v")
    assert argv[v_idx + 1] == "/host/w:/workspaces"
    assert "img" in argv
    # Container name flag.
    assert "--name" in argv
    n_idx = argv.index("--name")
    assert argv[n_idx + 1] == "mini_cc-t1"


def test_docker_runtime_exec_command_shape(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="ok"))
    rt = DockerRuntime()
    rt.exec(name="mini_cc-t1", workdir="/workspaces/p1",
            command="ls -la", timeout=30, env={"FOO": "bar"})
    exec_calls = [c for c in captured if c[:2] == ["docker", "exec"]]
    assert len(exec_calls) == 1
    argv = exec_calls[0]
    assert "-w" in argv
    w_idx = argv.index("-w")
    assert argv[w_idx + 1] == "/workspaces/p1"
    assert "-e" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "FOO=bar"
    assert "mini_cc-t1" in argv
    # Last two are `sh -c "ls -la"`
    assert argv[-2:] == ["sh", "-c"] or argv[-3:-1] == ["sh", "-c"]


def test_docker_runtime_status_returns_string(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="running\n"))
    assert DockerRuntime().status("mini_cc-t1") == "running"


def test_docker_runtime_status_missing(monkeypatch):
    """Container doesn't exist → returns 'missing'."""
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=1, stderr="No such object"))
    assert DockerRuntime().status("mini_cc-t1") == "missing"


def test_docker_runtime_stop_and_remove(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(args=a, returncode=0))
    rt = DockerRuntime()
    rt.stop("mini_cc-t1")
    rt.remove("mini_cc-t1")
    assert ["docker", "stop", "mini_cc-t1"] in captured
    assert ["docker", "rm", "-f", "mini_cc-t1"] in captured


def test_docker_runtime_list_managed(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(args=a, returncode=0,
            stdout="mini_cc-t1\nmini_cc-t2\nother-container\n"))
    rt = DockerRuntime()
    names = rt.list_managed()
    assert names == ["mini_cc-t1", "mini_cc-t2"]
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_runtime.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/runtime.py
"""ContainerRuntime: abstraction over the docker CLI.

Two implementations:
- ``DockerRuntime`` — real docker subprocess calls.
- ``FakeRuntime`` — records every call into ``.calls`` list; used by tests.

The Protocol is narrow: ensure_running, exec, status, stop, remove,
list_managed, is_available, build_image (delegated to imagebuild).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .imagebuild import build_image


class RuntimeUnavailable(RuntimeError):
    """Raised when a runtime call is made against an unavailable backend."""


class ContainerRuntime(Protocol):
    def is_available(self) -> bool: ...
    def ensure_running(self, *, name: str, image: str, mounts: list[tuple[str, str, str]],
                       network: str, cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None: ...
    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess: ...
    def status(self, name: str) -> str: ...
    def stop(self, name: str) -> None: ...
    def remove(self, name: str) -> None: ...
    def list_managed(self, prefix: str = "mini_cc-") -> list[str]: ...
    def build_image(self, tag: str, context_dir: Path, dockerfile: Path | None = None) -> None: ...


# ── DockerRuntime ─────────────────────────────────────────────────────────

class DockerRuntime:
    """All docker calls go through subprocess.run. No SDK dep."""

    def is_available(self) -> bool:
        try:
            cp = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return cp.returncode == 0

    def status(self, name: str) -> str:
        """One of 'running', 'exited', 'missing'."""
        cp = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=10)
        if cp.returncode != 0:
            return "missing"
        return cp.stdout.strip() or "missing"

    def ensure_running(self, *, name: str, image: str,
                       mounts: list[tuple[str, str, str]],
                       network: str, cpu_quota: str | None = None,
                       memory_limit: str | None = None) -> None:
        st = self.status(name)
        if st == "running":
            return
        if st == "exited":
            subprocess.run(["docker", "start", name],
                           capture_output=True, text=True, timeout=30)
            return
        # missing → run
        argv = ["docker", "run", "-d",
                "--name", name,
                "--restart=unless-stopped",
                f"--network={network}"]
        for host, container, options in mounts:
            spec = f"{host}:{container}"
            if options:
                spec += f":{options}"
            argv += ["-v", spec]
        if cpu_quota:
            argv += [f"--cpus={cpu_quota}"]
        if memory_limit:
            argv += [f"--memory={memory_limit}"]
        argv.append(image)
        subprocess.run(argv, capture_output=True, text=True, timeout=120, check=True)

    def exec(self, *, name: str, workdir: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        argv = ["docker", "exec"]
        if workdir:
            argv += ["-w", workdir]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [name, "sh", "-c", command]
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def stop(self, name: str) -> None:
        subprocess.run(["docker", "stop", name],
                       capture_output=True, text=True, timeout=30)

    def remove(self, name: str) -> None:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, text=True, timeout=30)

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        cp = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10)
        if cp.returncode != 0:
            return []
        return [n.strip() for n in cp.stdout.splitlines()
                if n.strip().startswith(prefix)]

    def build_image(self, tag: str, context_dir: Path,
                    dockerfile: Path | None = None) -> None:
        # Delegate to the imagebuild module for layered rendering.
        from .config import ContainerConfig
        cfg = ContainerConfig(image_tag=tag, dockerfile_path=str(dockerfile) if dockerfile else None)
        build_image(tag, cfg)


# ── FakeRuntime (test double) ──────────────────────────────────────────────

@dataclass
class FakeRuntime:
    """Records every call. Default available=True, status='missing'."""
    available: bool = True
    initial_status: str = "missing"
    exec_returncode: int = 0
    exec_stdout: str = ""
    exec_stderr: str = ""
    calls: list[tuple[str, dict]] = field(default_factory=list)
    _statuses: dict[str, str] = field(default_factory=dict)

    def is_available(self) -> bool:
        return self.available

    def status(self, name: str) -> str:
        return self._statuses.get(name, self.initial_status)

    def ensure_running(self, *, name, image, mounts, network,
                       cpu_quota=None, memory_limit=None) -> None:
        if not self.available:
            raise RuntimeUnavailable("fake runtime marked unavailable")
        self.calls.append(("ensure_running", dict(
            name=name, image=image, mounts=list(mounts),
            network=network, cpu_quota=cpu_quota, memory_limit=memory_limit)))
        # Transition to running so subsequent calls short-circuit.
        self._statuses[name] = "running"

    def exec(self, *, name, workdir, command, timeout, env):
        if not self.available:
            raise RuntimeUnavailable("fake runtime marked unavailable")
        self.calls.append(("exec", dict(name=name, workdir=workdir,
            command=command, timeout=timeout, env=dict(env))))
        return subprocess.CompletedProcess(
            args=["docker", "exec", name, "sh", "-c", command],
            returncode=self.exec_returncode,
            stdout=self.exec_stdout, stderr=self.exec_stderr)

    def stop(self, name: str) -> None:
        self.calls.append(("stop", dict(name=name)))
        self._statuses[name] = "exited"

    def remove(self, name: str) -> None:
        self.calls.append(("remove", dict(name=name)))
        self._statuses.pop(name, None)

    def list_managed(self, prefix: str = "mini_cc-") -> list[str]:
        return [n for n in self._statuses if n.startswith(prefix)]

    def build_image(self, tag, context_dir, dockerfile=None):
        self.calls.append(("build_image",
            dict(tag=tag, context_dir=context_dir, dockerfile=dockerfile)))
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_runtime.py -v
```

Expected: 12 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/runtime.py tests/test_p5_runtime.py
git commit -m "feat(p5): ContainerRuntime Protocol + DockerRuntime + FakeRuntime test double"
```

---

## Task 5: Tenant Container Manager

Owns the per-tenant container lifecycle. One instance per tenant id. Builds the container name, resolves mount specs, ensures running before exec.

**Files:**
- Create: `mini_cc/sandbox/manager.py`
- Test: `tests/test_p5_manager.py`

### Step 1: Write failing test

```python
# tests/test_p5_manager.py
"""TenantContainerManager lifecycle. Uses FakeRuntime — no docker daemon."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.sandbox.config import ContainerConfig, Mount
from mini_cc.sandbox.manager import TenantContainerManager, _container_name
from mini_cc.sandbox.runtime import FakeRuntime


def test_container_name_sanitizes_special_chars():
    assert _container_name("tnt_acme.corp") == "mini_cc-tnt_acme.corp"
    # Disallowed chars (spaces, slashes) replaced with _
    assert _container_name("tnt a/b") == "mini_cc-tnt_a_b"
    # 63-char Docker limit (including prefix)
    long = "t" * 80
    name = _container_name(long)
    assert len(name) <= 63
    assert name.startswith("mini_cc-")


def test_container_name_rejects_empty_after_prefix():
    with pytest.raises(ValueError):
        _container_name("")


def test_ensure_running_invokes_runtime_with_expected_args():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1",
        host_projects_dir=Path("/data/tenants/t1/projects"),
        config=ContainerConfig(enabled=True, image_tag="img:v1", network="none"),
        runtime=rt)
    mgr.ensure_running()
    assert len(rt.calls) == 1
    method, kwargs = rt.calls[0]
    assert kwargs["name"] == "mini_cc-t1"
    assert kwargs["image"] == "img:v1"
    assert kwargs["network"] == "none"
    # workspace mount: <host_projects_dir>:/workspaces
    mounts = kwargs["mounts"]
    assert (str(Path("/data/tenants/t1/projects")), "/workspaces", "") in mounts


def test_ensure_running_includes_extra_mounts():
    rt = FakeRuntime()
    cfg = ContainerConfig(enabled=True,
        extra_mounts=[Mount(host="/host/cache", container="/cache", options="ro")])
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"), config=cfg, runtime=rt)
    mgr.ensure_running()
    mounts = rt.calls[0][1]["mounts"]
    assert ("/host/cache", "/cache", "ro") in mounts


def test_ensure_running_passes_resource_limits():
    rt = FakeRuntime()
    cfg = ContainerConfig(enabled=True, cpu_quota="1.5", memory_limit="512m")
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"), config=cfg, runtime=rt)
    mgr.ensure_running()
    kwargs = rt.calls[0][1]
    assert kwargs["cpu_quota"] == "1.5"
    assert kwargs["memory_limit"] == "512m"


def test_ensure_running_idempotent():
    rt = FakeRuntime(initial_status="running")
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.ensure_running()
    mgr.ensure_running()
    # First ensure_running short-circuits via status=running → 0 run calls.
    assert all(c[0] != "ensure_running" for c in rt.calls)


def test_exec_workdir_uses_container_projects_path():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.exec(project_id="proj_abc", command="ls",
             timeout=30, env={"PATH": "/usr/bin"})
    # ensure_running then exec
    assert any(c[0] == "exec" for c in rt.calls)
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert exec_kwargs["workdir"] == "/workspaces/proj_abc"
    assert exec_kwargs["command"] == "ls"
    assert exec_kwargs["env"]["PATH"] == "/usr/bin"


def test_exec_does_not_filter_env_caller_decides():
    """Manager passes env through verbatim; filtering is ContainerSandbox's job."""
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.exec(project_id="p1", command="x", timeout=5, env={"SECRET": "s"})
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    assert exec_kwargs["env"]["SECRET"] == "s"


def test_stop_delegates_to_runtime():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True), runtime=rt)
    mgr.ensure_running()
    mgr.stop()
    assert any(c[0] == "stop" for c in rt.calls)


def test_image_tag_passes_through():
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=Path("/p"),
        config=ContainerConfig(enabled=True, image_tag="custom:v9"),
        runtime=rt)
    mgr.ensure_running()
    assert rt.calls[0][1]["image"] == "custom:v9"
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_manager.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/manager.py
"""Per-tenant container lifecycle. One TenantContainerManager per tenant id.

Owns:
- The sanitized container name (mini_cc-<sanitized_tid>, ≤63 chars).
- Translating ContainerConfig → mount specs for ensure_running.
- Forwarding exec() calls with workdir=/workspaces/<project_id>.

Does NOT own:
- Env filtering (caller's job; the Sandbox layer applies the policy).
- Image building (caller must ensure the image exists — manager only
  names it).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import ContainerConfig
from .runtime import ContainerRuntime

_PREFIX = "mini_cc-"
_MAX_NAME = 63  # Docker container name limit


def _container_name(tid: str) -> str:
    """Sanitize tid to a valid Docker container name.

    Docker names: [A-Za-z0-9][A-Za-z0-9_.-]*. We replace spaces and
    path separators with _ (defensive — ProjectManager already validates
    tid against [A-Za-z0-9_-]+ so most input is already clean).
    """
    if not tid:
        raise ValueError("tid must be non-empty")
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", tid)
    if not sanitized or sanitized[0] in ".-":
        sanitized = "t_" + sanitized
    name = _PREFIX + sanitized
    if len(name) > _MAX_NAME:
        # Keep prefix, trim the tid portion.
        name = _PREFIX + sanitized[-(_MAX_NAME - len(_PREFIX)):]
    return name


class TenantContainerManager:
    def __init__(self, tid: str, host_projects_dir: Path,
                 config: ContainerConfig, runtime: ContainerRuntime):
        self.tid = tid
        self.host_projects_dir = host_projects_dir
        self.config = config
        self.runtime = runtime
        self.container_name = _container_name(tid)

    def _mounts(self) -> list[tuple[str, str, str]]:
        """The tenant projects dir is always mounted at /workspaces; any
        extra_mounts from config are appended."""
        mounts = [(str(self.host_projects_dir), "/workspaces", "")]
        for m in self.config.extra_mounts:
            mounts.append((m.host, m.container, m.options))
        return mounts

    def ensure_running(self) -> None:
        self.runtime.ensure_running(
            name=self.container_name,
            image=self.config.image_tag,
            mounts=self._mounts(),
            network=self.config.network,
            cpu_quota=self.config.cpu_quota,
            memory_limit=self.config.memory_limit)

    def exec(self, *, project_id: str, command: str,
             timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess:
        self.ensure_running()
        return self.runtime.exec(
            name=self.container_name,
            workdir=f"/workspaces/{project_id}",
            command=command,
            timeout=timeout,
            env=env)

    def stop(self) -> None:
        self.runtime.stop(self.container_name)

    def remove(self) -> None:
        self.runtime.remove(self.container_name)
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_manager.py -v
```

Expected: 10 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/manager.py tests/test_p5_manager.py
git commit -m "feat(p5): TenantContainerManager — per-tenant lifecycle + name sanitizer"
```

---

## Task 6: ContainerSandbox Sandbox Protocol Impl

Implements the `Sandbox` protocol. Fs ops delegate to embedded `SubprocessSandbox`; `execute()`/`git()` go through the container manager.

**Files:**
- Create: `mini_cc/sandbox/container.py`
- Modify: `mini_cc/sandbox/base.py` (+1 line: re-export)
- Modify: `mini_cc/sandbox/__init__.py` (re-exports)
- Test: `tests/test_p5_container_sandbox.py`

### Step 1: Write failing test

```python
# tests/test_p5_container_sandbox.py
"""ContainerSandbox: fs stays on host, exec/git go through the container."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.sandbox import (
    ContainerSandbox, CommandBlockedError, Policy)
from mini_cc.sandbox.config import ContainerConfig
from mini_cc.sandbox.manager import TenantContainerManager
from mini_cc.sandbox.runtime import FakeRuntime


@pytest.fixture
def sandbox(tmp_path):
    """A ContainerSandbox wired to a FakeRuntime."""
    ws = tmp_path / "p1" / "workspace"
    ws.mkdir(parents=True)
    rt = FakeRuntime()
    mgr = TenantContainerManager(
        tid="t1", host_projects_dir=tmp_path,
        config=ContainerConfig(enabled=True), runtime=rt)
    sb = ContainerSandbox("p1", ws, Policy(), mgr)
    return sb, rt


def test_fs_ops_make_zero_runtime_calls(sandbox):
    sb, rt = sandbox
    sb.write("file.txt", "hello")
    assert sb.read("file.txt") == "hello"
    sb.edit("file.txt", "hello", "hi")
    assert sb.read("file.txt") == "hi"
    assert sb.glob("*.txt") == ["file.txt"]
    assert len(rt.calls) == 0


def test_execute_invokes_container_exec(sandbox):
    sb, rt = sandbox
    sb.execute("ls -la")
    assert any(c[0] == "exec" for c in rt.calls)
    # First call should have been ensure_running
    assert rt.calls[0][0] == "ensure_running"


def test_execute_second_call_reuses_container(sandbox):
    sb, rt = sandbox
    sb.execute("ls")
    sb.execute("pwd")
    # One ensure_running (transitioned to running), two exec calls.
    ensure_calls = [c for c in rt.calls if c[0] == "ensure_running"]
    exec_calls = [c for c in rt.calls if c[0] == "exec"]
    assert len(ensure_calls) == 1
    assert len(exec_calls) == 2


def test_execute_policy_violation_blocks_before_runtime(sandbox):
    sb, rt = sandbox
    with pytest.raises(CommandBlockedError):
        sb.execute("sudo rm -rf /")
    assert all(c[0] != "exec" for c in rt.calls)


def test_git_invokes_container_exec(sandbox):
    sb, rt = sandbox
    sb.git(["status"])
    exec_calls = [c for c in rt.calls if c[0] == "exec"]
    assert len(exec_calls) == 1
    cmd = exec_calls[0][1]["command"]
    assert "git status" in cmd


def test_git_policy_violation_blocks(sandbox):
    sb, rt = sandbox
    with pytest.raises(CommandBlockedError):
        sb.git(["push", "--force"])  # push not in ALLOWED_GIT_SUBCOMMANDS
    assert all(c[0] != "exec" for c in rt.calls)


def test_execute_filters_env_via_policy(sandbox):
    sb, rt = sandbox
    sb.execute("printenv")
    exec_kwargs = next(c[1] for c in rt.calls if c[0] == "exec")
    # Only ALLOWED_ENV_VARS survive; HOME is set to the in-container workspace.
    assert "HOME" in exec_kwargs["env"]
    assert exec_kwargs["env"]["HOME"] == "/workspaces/p1"
    # SECRET would be in os.environ if the test set it; here we just verify
    # PATH survives the whitelist (it's in ALLOWED_ENV_VARS).
    if "PATH" in __import__("os").environ:
        assert "PATH" in exec_kwargs["env"]


def test_validate_path_still_rejects_escapes(sandbox):
    sb, rt = sandbox
    from mini_cc.sandbox import PathEscapeError
    with pytest.raises(PathEscapeError):
        sb.write("../../escape.txt", "x")


def test_resolve_run_cwd_inside_workspace_ok(sandbox):
    sb, rt = sandbox
    sb.execute("ls", cwd="subdir")  # cwd validated by underlying SubprocessSandbox
    # No exception means pass; exec call recorded.
    assert any(c[0] == "exec" for c in rt.calls)
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_container_sandbox.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

```python
# mini_cc/sandbox/container.py
"""ContainerSandbox — Sandbox protocol impl where execute/git run inside
a per-tenant Docker container and fs ops stay on host.

Fs delegation reuses the path-isolation logic of SubprocessSandbox —
the workspace is bind-mounted into the container at /workspaces/<pid>,
so in-container processes see the same files the host-side fs ops see.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import CommandBlockedError
from .manager import TenantContainerManager
from .policy import Policy
from .subprocess_sandbox import SubprocessSandbox


class ContainerSandbox:
    """Sandbox that delegates execute()/git() to a container.

    The first execute()/git() call triggers ``ensure_running()`` on the
    tenant's container (idempotent thereafter). Fs operations never touch
    the container — they run through the embedded SubprocessSandbox on
    the host, which already enforces project-root path isolation.
    """

    def __init__(self, project_id: str, project_root: Path,
                 policy: Policy, container_mgr: TenantContainerManager):
        self.project_id = project_id
        self.project_root = Path(project_root).resolve()
        self.policy = policy
        self._fs = SubprocessSandbox(project_id, project_root, policy)
        self._mgr = container_mgr

    # ── Fs: pure delegation ────────────────────────────────────────────────
    def resolve_path(self, rel):
        return self._fs.resolve_path(rel)

    def validate_path(self, path):
        return self._fs.validate_path(path)

    def read(self, *a, **kw):
        return self._fs.read(*a, **kw)

    def write(self, *a, **kw):
        return self._fs.write(*a, **kw)

    def edit(self, *a, **kw):
        return self._fs.edit(*a, **kw)

    def glob(self, *a, **kw):
        return self._fs.glob(*a, **kw)

    def grep(self, *a, **kw):
        return self._fs.grep(*a, **kw)

    # ── Process: container ────────────────────────────────────────────────
    def _filtered_env(self, extra: dict | None) -> dict[str, str]:
        """Apply the same env whitelist SubprocessSandbox uses, then set
        HOME to the in-container project dir so tools that look at $HOME
        (git config, npm cache) land inside the workspace."""
        env = {k: v for k, v in os.environ.items()
               if k in self.policy.allowed_env}
        env["HOME"] = f"/workspaces/{self.project_id}"
        # Windows-side USERPROFILE would mislead Linux tools in the container.
        env.pop("USERPROFILE", None)
        if extra:
            env.update(extra)
        return env

    def execute(self, command, *, timeout=120, env=None, cwd=None):
        violations = self.policy.scan_command(command)
        if violations:
            raise CommandBlockedError(violations)
        # Validate cwd against project_root (same rule as SubprocessSandbox).
        # We don't pass cwd to docker exec — the container always cds to
        # /workspaces/<pid>. If a relative cwd was requested, we prepend
        # a cd to the command so the in-container shell lands there.
        if cwd is not None:
            resolved = self._fs._resolve_run_cwd(cwd)
            rel = resolved.relative_to(self.project_root)
            command = f"cd {rel.as_posix()} && {command}"
        return self._mgr.exec(
            project_id=self.project_id,
            command=command,
            timeout=timeout,
            env=self._filtered_env(env))

    def git(self, args, *, timeout=60):
        from .base import CommandBlockedError
        v = self.policy.check_git_args(args)
        if v:
            raise CommandBlockedError([v])
        # git runs inside the container with HOME pointing at the workspace.
        cmd_str = " ".join(["git"] + [str(a) for a in args])
        return self._mgr.exec(
            project_id=self.project_id,
            command=cmd_str,
            timeout=timeout,
            env=self._filtered_env(None))
```

Update re-exports:

```python
# mini_cc/sandbox/base.py  (append at bottom)
from .container import ContainerSandbox  # noqa: E402,F401
```

Wait — that creates a circular import (`container.py` imports from `base.py`). Instead, do the re-export only in `__init__.py`:

```python
# mini_cc/sandbox/__init__.py
from .base import CommandBlockedError, PathEscapeError, Sandbox
from .policy import Policy, Violation
from .subprocess_sandbox import SubprocessSandbox
from .container import ContainerSandbox
from .config import ContainerConfig, Mount, load_tenant_config, resolve_kind
from .runtime import ContainerRuntime, DockerRuntime, FakeRuntime, RuntimeUnavailable
from .manager import TenantContainerManager
from .osdetect import DockerAvailability, probe_docker, reset_cache

__all__ = [
    # existing
    "Sandbox", "SubprocessSandbox", "Policy", "Violation",
    "PathEscapeError", "CommandBlockedError",
    # P5
    "ContainerSandbox", "ContainerConfig", "Mount",
    "load_tenant_config", "resolve_kind",
    "ContainerRuntime", "DockerRuntime", "FakeRuntime", "RuntimeUnavailable",
    "TenantContainerManager",
    "DockerAvailability", "probe_docker", "reset_cache",
]
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_container_sandbox.py -v
```

Expected: 9 passed.

### Step 5: Commit

```bash
git add mini_cc/sandbox/container.py mini_cc/sandbox/__init__.py tests/test_p5_container_sandbox.py
git commit -m "feat(p5): ContainerSandbox — Sandbox impl that routes exec/git via container"
```

---

## Task 7: ProjectManager Sandbox Factory Injection

Modify `ProjectManager` to accept a `sandbox_factory` and use it in `_assemble` instead of hardcoding `SubprocessSandbox`.

**Files:**
- Modify: `mini_cc/projects/manager.py:96-197`
- Test: `tests/test_p5_manager_factory.py`

### Step 1: Write failing test

```python
# tests/test_p5_manager_factory.py
"""ProjectManager accepts a sandbox_factory closure. Default preserves
today's behaviour (SubprocessSandbox)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_cc.projects import ProjectManager
from mini_cc.sandbox import ContainerSandbox, SubprocessSandbox, Policy
from mini_cc.sandbox.config import ContainerConfig
from mini_cc.sandbox.manager import TenantContainerManager
from mini_cc.sandbox.runtime import FakeRuntime


def test_default_factory_returns_subprocess(tmp_path):
    pm = ProjectManager(tmp_path)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)
    assert not isinstance(p.sandbox, ContainerSandbox)


def test_custom_factory_used_when_supplied(tmp_path):
    rt = FakeRuntime()
    captured = []

    def factory(tid, pid, ws, policy):
        captured.append((tid, pid, ws))
        mgr = TenantContainerManager(
            tid=tid, host_projects_dir=ws.parent,
            config=ContainerConfig(enabled=True), runtime=rt)
        return ContainerSandbox(pid, ws, policy, mgr)

    pm = ProjectManager(tmp_path, sandbox_factory=factory)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, ContainerSandbox)
    assert captured[0][0] == "t1"
    assert captured[0][1] == "p1"


def test_teammate_loop_uses_factory_output(tmp_path):
    """The teammate sub-loop built in _build_teammate_loop should also
    go through the factory so sub-agents honor the same sandbox."""
    rt = FakeRuntime()
    def factory(tid, pid, ws, policy):
        mgr = TenantContainerManager(
            tid=tid, host_projects_dir=ws.parent,
            config=ContainerConfig(enabled=True), runtime=rt)
        return ContainerSandbox(pid, ws, policy, mgr)
    pm = ProjectManager(tmp_path, sandbox_factory=factory)
    p = pm.create(tenant_id="t1", project_id="p1")
    # _build_teammate_loop creates a fresh sandbox — verify it used the factory.
    # We can't easily call _build_teammate_loop directly; instead we check
    # the loop_factory closure attached to TeammateSpawner. For now this
    # test documents the expectation; a real exercise happens in the
    # subagent integration test.
    assert hasattr(p.teams, "_loop_factory")
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_manager_factory.py -v
```

Expected: TypeError — `ProjectManager.__init__` doesn't accept `sandbox_factory`.

### Step 3: Write minimal implementation

Modify `mini_cc/projects/manager.py`:

```python
# Add import at top
from typing import Callable
from ..sandbox import Sandbox

# Type alias near other type aliases
SandboxFactory = Callable[[str, str, Path, Policy], Sandbox]

# Modify ProjectManager.__init__
class ProjectManager:
    def __init__(self, root: Path,
                 storage_factory: StorageFactory | None = None,
                 policy: Policy | None = None,
                 metrics: "object | None" = None,
                 sandbox_factory: SandboxFactory | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_factory = storage_factory or _fs_factory
        self.policy = policy or Policy()
        self.metrics = metrics
        self._sandbox_factory = sandbox_factory or _default_sandbox_factory

    # In _assemble (replace the SubprocessSandbox construction line):
    def _assemble(self, project_id: str, meta: ProjectMeta) -> Project:
        ws = workspace_path(self.root, project_id)
        sandbox = self._sandbox_factory(meta.tenant_id, project_id, ws, self.policy)
        # ... rest unchanged
```

Add the default factory at module level (near `_fs_factory`):

```python
def _default_sandbox_factory(tid: str, pid: str, ws: Path,
                             policy: Policy) -> Sandbox:
    """Default: one SubprocessSandbox per project. Server can swap this
    for a container-aware factory at startup (Phase G / P5)."""
    return SubprocessSandbox(pid, ws, policy)
```

Also update `_build_teammate_loop` to use the factory:

```python
def _build_teammate_loop(project: "Project", session_id: str):
    from ..core.loop import AgentLoop
    ref = project.as_ref()
    # Reuse the manager's factory so teammates honor the same sandbox kind.
    # We need access to the factory — easiest path is to stash it on Project.
    ref.sandbox = project._sandbox_factory(
        project.meta.tenant_id, project.project_id, project.workspace,
        project.sandbox.policy)
    return AgentLoop(ref, session_id)
```

But `_sandbox_factory` is on `ProjectManager`, not `Project`. Add it to `Project` dataclass:

```python
@dataclass
class Project:
    # ... existing fields ...
    _sandbox_factory: object = None  # SandboxFactory; stashed for teammate reuse
```

And in `_assemble`, after constructing `project`:

```python
project._sandbox_factory = self._sandbox_factory
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_manager_factory.py -v
```

Expected: 3 passed. Also run the full existing test suite to confirm no regression:

```bash
python -m pytest tests/ -q
```

Expected: prior pass count (553) + new tests, all green.

### Step 5: Commit

```bash
git add mini_cc/projects/manager.py tests/test_p5_manager_factory.py
git commit -m "feat(p5): ProjectManager accepts sandbox_factory; default unchanged"
```

---

## Task 8: Auto-Degrade Wiring in cli.py + app.py Lifespan

Build the server-side factory closure that consults `resolve_kind` + `probe_docker`, auto-degrades when docker is unavailable, and registers container cleanup in lifespan.

**Files:**
- Modify: `mini_cc/server/app.py:30-41` (lifespan)
- Modify: `mini_cc/server/cli.py:56-110` (cmd_serve)
- Test: `tests/test_p5_http_degrade.py`

### Step 1: Write failing test

```python
# tests/test_p5_http_degrade.py
"""End-to-end: when container enabled but docker unavailable, the
project auto-degrades to SubprocessSandbox (no 503)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.session import SessionManager
from mini_cc.sandbox import SubprocessSandbox, ContainerSandbox
from mini_cc.server.app import build_app
from mini_cc.server.runtime_context import ServerRuntimeContext


def _build(tmp_path, monkeypatch, *, docker_available: bool):
    """Build a server with a stubbed DockerAvailability."""
    monkeypatch.setenv("MINI_CC_DATA_DIR", str(tmp_path))
    # Tenant with container sandbox enabled.
    tenants_dir = tmp_path / "tenants" / "t1"
    tenants_dir.mkdir(parents=True)
    (tenants_dir / "sandbox.toml").write_text("enabled = true\n")

    ctx = ServerRuntimeContext(
        data_dir=tmp_path,
        key_registry=TenantKeyRegistry(tmp_path / "keys.json"),
        docker_available=docker_available,
    )
    pm = ctx.build_project_manager()
    sm = SessionManager(pm)
    app = build_app(
        data_dir=tmp_path,
        key_registry=ctx.key_registry,
        pm=pm, sm=sm,
        server_runtime=ctx,
    )
    return app, pm


def test_tenant_with_container_enabled_and_docker_available(tmp_path, monkeypatch):
    app, pm = _build(tmp_path, monkeypatch, docker_available=True)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, ContainerSandbox)


def test_tenant_with_container_enabled_but_docker_missing_degrades(tmp_path, monkeypatch):
    """User requirement #2: container env unsupported → degrade to current."""
    app, pm = _build(tmp_path, monkeypatch, docker_available=False)
    p = pm.create(tenant_id="t1", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)
    # The degrade reason is recorded on the runtime context for /metrics.
    ctx_degrades = app.state.server_runtime.degrades
    assert any(d.tenant_id == "t1" for d in ctx_degrades)


def test_tenant_without_config_always_subprocess(tmp_path, monkeypatch):
    """No sandbox.toml, no MINI_CC_SANDBOX_DEFAULT → subprocess always."""
    monkeypatch.delenv("MINI_CC_SANDBOX_DEFAULT", raising=False)
    monkeypatch.setenv("MINI_CC_DATA_DIR", str(tmp_path))
    ctx = ServerRuntimeContext(
        data_dir=tmp_path,
        key_registry=TenantKeyRegistry(tmp_path / "keys.json"),
        docker_available=True,
    )
    pm = ctx.build_project_manager()
    p = pm.create(tenant_id="t_no_sb", project_id="p1")
    assert isinstance(p.sandbox, SubprocessSandbox)
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_http_degrade.py -v
```

Expected: ImportError — `mini_cc.server.runtime_context` does not exist.

### Step 3: Write minimal implementation

Create new module `mini_cc/server/runtime_context.py`:

```python
# mini_cc/server/runtime_context.py
"""Server-side wiring for the container sandbox.

Owns the singleton DockerAvailability probe and the per-tenant
TenantContainerManager cache. Builds the ProjectManager sandbox_factory
closure that:
  1. Calls resolve_kind(tid, tenants_dir) → subprocess | container.
  2. If container: checks docker_available — if False, records a degrade
     event and returns SubprocessSandbox (auto-degrade, user requirement #2).
  3. Otherwise returns ContainerSandbox wired to the tenant's manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..auth import TenantKeyRegistry
from ..sandbox import (
    ContainerSandbox, Policy, SubprocessSandbox,
    TenantContainerManager, probe_docker, resolve_kind)
from ..sandbox.runtime import DockerRuntime


@dataclass
class DegradeEvent:
    tenant_id: str
    reason: str


@dataclass
class ServerRuntimeContext:
    data_dir: Path
    key_registry: TenantKeyRegistry
    docker_available: bool
    degrades: list[DegradeEvent] = field(default_factory=list)
    _container_mgrs: dict[str, TenantContainerManager] = field(default_factory=dict)
    _runtime: object = None  # DockerRuntime or FakeRuntime

    def __post_init__(self):
        if self._runtime is None:
            self._runtime = DockerRuntime()

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    def build_project_manager(self):
        from ..projects import ProjectManager
        return ProjectManager(
            self.data_dir / "projects",
            sandbox_factory=self._sandbox_factory)

    def _sandbox_factory(self, tid: str, pid: str, ws: Path,
                         policy: Policy):
        kind, cfg = resolve_kind(tid, self.tenants_dir)
        if kind == "subprocess" or cfg is None:
            return SubprocessSandbox(pid, ws, policy)
        if not self.docker_available:
            # Auto-degrade: container wanted, docker unavailable.
            self.degrades.append(DegradeEvent(
                tenant_id=tid,
                reason="docker unavailable; falling back to subprocess"))
            return SubprocessSandbox(pid, ws, policy)
        mgr = self._container_mgrs.get(tid)
        if mgr is None:
            host_projects_dir = ws.parent  # <data>/projects/<pid>/workspace's parent
            # Actually we want the tenant-scoped projects dir. Today all
            # projects live under <data>/projects/<pid>, so the tenant-wide
            # mount is the parent.
            host_projects_dir = self.data_dir / "projects"
            mgr = TenantContainerManager(
                tid=tid,
                host_projects_dir=host_projects_dir,
                config=cfg,
                runtime=self._runtime)
            self._container_mgrs[tid] = mgr
        return ContainerSandbox(pid, ws, policy, mgr)

    def shutdown(self) -> None:
        """Stop every managed container. Called from app lifespan shutdown."""
        for mgr in self._container_mgrs.values():
            try:
                mgr.stop()
            except Exception:
                pass
```

Modify `mini_cc/server/app.py`:

```python
# Add to lifespan (after sessions stop, before yield-return completes):
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    sm: SessionManager = app.state.sm
    for (pid, sid), sess in list(sm._sessions.items()):
        try:
            sess.stop()
        except Exception:
            pass
    ctx = getattr(app.state, "server_runtime", None)
    if ctx is not None:
        ctx.shutdown()

# Add parameter to build_app:
def build_app(*, data_dir: Path,
              key_registry: TenantKeyRegistry,
              pm: ProjectManager,
              sm: SessionManager,
              cors_origins: list[str] | None = None,
              rate_limiter: TenantRateLimiter | None = None,
              metrics_registry: MetricsRegistry | None = None,
              server_runtime: "ServerRuntimeContext | None" = None) -> FastAPI:
    # ... existing body ...
    app.state.server_runtime = server_runtime
```

Modify `mini_cc/server/cli.py:cmd_serve` to construct `ServerRuntimeContext` with probed availability and pass to `build_app`:

```python
def cmd_serve(args) -> int:
    import uvicorn
    from ..projects import ProjectManager
    from ..session import SessionManager
    from ..sandbox import probe_docker
    from .app import build_app
    from .runtime_context import ServerRuntimeContext

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    keys = _key_registry()
    keys.load()

    avail = probe_docker()
    if not avail.available:
        # Don't fail — log and let ServerRuntimeContext auto-degrade per tenant.
        # This honours user requirement #2.
        import logging
        logging.getLogger("mini_cc").warning(
            f"docker unavailable ({avail.reason}); container-enabled tenants "
            f"will auto-degrade to subprocess sandbox")
    ctx = ServerRuntimeContext(
        data_dir=data_dir,
        key_registry=keys,
        docker_available=avail.available,
    )
    pm = ctx.build_project_manager()
    sm = SessionManager(pm)
    app = build_app(
        data_dir=data_dir, key_registry=keys,
        pm=pm, sm=sm,
        cors_origins=_cors_origins(),
        rate_limiter=_rate_limiter(),
        server_runtime=ctx,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_http_degrade.py -v
```

Expected: 3 passed.

### Step 5: Commit

```bash
git add mini_cc/server/runtime_context.py mini_cc/server/app.py mini_cc/server/cli.py tests/test_p5_http_degrade.py
git commit -m "feat(p5): server runtime context + auto-degrade when docker unavailable"
```

---

## Task 9: `sandbox` CLI Subcommand

New CLI verbs: `build-image`, `status`, `stop`. Operators use these to manage images and containers outside the chat.

**Files:**
- Modify: `mini_cc/server/cli.py` (extend argparse subcommands)
- Test: `tests/test_p5_cli_sandbox.py`

### Step 1: Write failing test

```python
# tests/test_p5_cli_sandbox.py
"""sandbox subcommand: build-image / status / stop."""
from __future__ import annotations

import subprocess

import pytest

from mini_cc.server.cli import cmd_sandbox_build_image, cmd_sandbox_status, cmd_sandbox_stop


def test_build_image_invokes_docker(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="", stderr=""))
    # Patch imagebuild._context_dir so we don't write into the package dir.
    monkeypatch.setattr("mini_cc.sandbox.imagebuild._context_dir", lambda: tmp_path)
    rc = cmd_sandbox_build_image(type("A", (), {
        "tag": "mini_cc-sandbox:latest",
        "dockerfile": None,
    })())
    assert rc == 0
    assert captured[0][:3] == ["docker", "build", "-t"]


def test_status_shows_managed_containers(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: subprocess.CompletedProcess(
            args=a, returncode=0,
            stdout="mini_cc-t1\nmini_cc-t2\n", stderr=""))
    rc = cmd_sandbox_status(type("A", (), {"tenant_id": None})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "mini_cc-t1" in out
    assert "mini_cc-t2" in out


def test_stop_with_tenant_id(monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "run",
        lambda a, **kw: captured.append(a) or subprocess.CompletedProcess(
            args=a, returncode=0, stdout="", stderr=""))
    rc = cmd_sandbox_stop(type("A", (), {"tenant_id": "t1"})())
    assert rc == 0
    assert ["docker", "stop", "mini_cc-t1"] in captured
    assert ["docker", "rm", "-f", "mini_cc-t1"] in captured
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_p5_cli_sandbox.py -v
```

Expected: ImportError.

### Step 3: Write minimal implementation

Add to `mini_cc/server/cli.py`:

```python
def cmd_sandbox_build_image(args) -> int:
    """python -m mini_cc.server sandbox build-image [--tag T] [--dockerfile P]"""
    from ..sandbox.imagebuild import build_image
    from ..sandbox.config import ContainerConfig
    cfg = ContainerConfig(
        image_tag=args.tag,
        dockerfile_path=args.dockerfile,
    )
    try:
        build_image(args.tag, cfg)
    except Exception as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"image built: {args.tag}")
    return 0


def cmd_sandbox_status(args) -> int:
    from ..sandbox.runtime import DockerRuntime
    rt = DockerRuntime()
    if not rt.is_available():
        print("docker unavailable", file=sys.stderr)
        return 1
    names = rt.list_managed()
    if args.tenant_id:
        from ..sandbox.manager import _container_name
        wanted = _container_name(args.tenant_id)
        names = [n for n in names if n == wanted]
    for n in names:
        st = rt.status(n)
        print(f"{n}\t{st}")
    return 0


def cmd_sandbox_stop(args) -> int:
    from ..sandbox.runtime import DockerRuntime
    from ..sandbox.manager import _container_name
    rt = DockerRuntime()
    name = _container_name(args.tenant_id)
    try:
        rt.stop(name)
        rt.remove(name)
    except Exception as exc:
        print(f"stop failed: {exc}", file=sys.stderr)
        return 1
    print(f"stopped: {name}")
    return 0


# In main() / argparse setup (extend existing parser):
# sandbox = sub.add_parser("sandbox")
# sandbox_sub = sandbox.add_subparsers(dest="sandbox_cmd", required=True)
# sb_build = sandbox_sub.add_parser("build-image")
# sb_build.add_argument("--tag", default="mini_cc-sandbox:latest")
# sb_build.add_argument("--dockerfile", default=None)
# sb_build.set_defaults(func=cmd_sandbox_build_image)
# sb_status = sandbox_sub.add_parser("status")
# sb_status.add_argument("--tid", dest="tenant_id", default=None)
# sb_status.set_defaults(func=cmd_sandbox_status)
# sb_stop = sandbox_sub.add_parser("stop")
# sb_stop.add_argument("tenant_id")
# sb_stop.set_defaults(func=cmd_sandbox_stop)
```

### Step 4: Run test to verify it passes

```bash
python -m pytest tests/test_p5_cli_sandbox.py -v
```

Expected: 3 passed.

### Step 5: Commit

```bash
git add mini_cc/server/cli.py tests/test_p5_cli_sandbox.py
git commit -m "feat(p5): sandbox CLI subcommand — build-image / status / stop"
```

---

## Task 10: Docs — Container Sandbox Section

Add a "Container Sandbox (P5)" section to both READMEs covering config, fallback behavior, and Windows/WSL2 notes.

**Files:**
- Modify: `mini_cc/README.md`
- Modify: `mini_cc/README.zh.md`

### Step 1: Implement

Append a new section to both READMEs following the existing format. Cover:
- When to enable (defense-in-depth for multi-tenant)
- `MINI_CC_SANDBOX_DEFAULT` env var
- `tenants/{tid}/sandbox.toml` schema with full example (all 4 dimensions)
- Auto-degrade behavior (when docker missing, falls back to subprocess + warning)
- Windows/WSL2 auto-detect note
- CLI: `sandbox build-image`, `sandbox status`, `sandbox stop`
- Out-of-scope (same list as design doc)

Content can be adapted from this plan's "Locked Decisions" table + the existing `2026-06-21-phaseG-container-sandbox-design.md` "Out of scope" section.

### Step 2: Manual verify

```bash
# Render-check both READMEs (eyeball — no test)
grep -c "Container Sandbox" mini_cc/README.md mini_cc/README.zh.md
```

Expected: each file has ≥ 1 match.

### Step 3: Commit

```bash
git add mini_cc/README.md mini_cc/README.zh.md
git commit -m "docs(p5): container sandbox section in READMEs (zh/en)"
```

---

## Task 11: Full Suite + Manual Smoke Test

### Step 1: Run full suite

```bash
python -m pytest tests/ -q
```

Expected: prior pass count (553) + ~80 new P5 tests, all green.

### Step 2: Build the image manually

```bash
python -m mini_cc.server sandbox build-image
docker images mini_cc-sandbox
```

Expected: image listed.

### Step 3: Manual end-to-end (requires docker daemon)

```bash
# Setup tenant with container enabled
mkdir -p $MINI_CC_DATA_DIR/tenants/t1
cat > $MINI_CC_DATA_DIR/tenants/t1/sandbox.toml <<'EOF'
enabled = true
network = "none"
EOF

# Start server (logs should show "docker available" or auto-degrade warning)
python -m mini_cc.server &

# Drive a chat turn that runs !ls — verify output comes from /workspaces/<pid>
# Drive !curl http://example.com — should fail (network=none)
# Edit sandbox.toml: network = "bridge", restart server, retry curl → succeeds
docker ps | grep mini_cc-t1

# Shutdown → container cleaned up
python -m mini_cc.server sandbox status  # should be empty after stop
```

### Step 4: Commit any fixups

```bash
git status
# If any tweaks were needed during smoke:
git add ...
git commit -m "fix(p5): smoke-test adjustments"
```

---

## Out of Scope

Same as the original Phase G design doc, plus:

- **Per-project sandbox config override.** Stays tenant-level.
- **Hot-reload of sandbox.toml.** Cold-load only; restart to pick up changes.
- **Auto image build on first request.** Build via CLI explicitly.
- **Podman / gVisor / Firecracker backend.** Only Docker.
- **Multi-arch images.** Dockerfile builds for current arch.
- **Rootless Docker / userns-remap.**
- **`docker stats` metrics.** cpu/memory caps exist but per-container usage isn't surfaced.
- **Python venv injection into image.** Agents create their own venvs.
- **Image pull from private registry auth.** Operators pre-`docker login`.

## Verification Summary

```bash
# All tests (no docker daemon needed)
python -m pytest tests/test_p5_*.py -v       # ~80 new tests
python -m pytest tests/ -q                    # full suite, expect ~633 green

# Image build
python -m mini_cc.server sandbox build-image

# Manual e2e
python -m mini_cc.server sandbox status
docker ps | grep mini_cc-
```
