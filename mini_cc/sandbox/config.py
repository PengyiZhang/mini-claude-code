"""ContainerConfig + per-tenant sandbox.toml loader.

Two-layer config:
- Server default: ``MINI_CC_SANDBOX_DEFAULT=subprocess|container`` env var.
  Absent → ``subprocess`` (today's behavior unchanged).
- Tenant override: ``<tenants_dir>/<tid>/sandbox.toml``.

``sandbox.toml`` schema (all fields optional)::

    enabled = true
    image_tag = "my-registry/sb:v2"
    network = "none"                    # "none" | "bridge"
    dockerfile_path = "./Dockerfile.x"
    cpu_quota = "1.5"
    memory_limit = "512m"

    apt_packages = ["ffmpeg"]
    pip_packages = ["numpy"]
    node_packages = ["typescript"]

    [[extra_mounts]]
    host = "/host/cache"
    container = "/cache"
    options = "ro"

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
    When env-default=container and tenant file is absent, the returned
    cfg has ``enabled=True`` (server default implies on).
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
