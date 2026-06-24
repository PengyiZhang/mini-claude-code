"""3-tier plugin directory layout + bootstrap.

Every tier has the same on-disk shape::

    <tier_root>/.mini_cc/
        skills/
            <skill-name>/SKILL.md
        mcp.toml
        permissions.toml

Tiers:

- **System** — ``<data_dir>/.mini_cc/``. Owned by the operator; applies
  to every tenant and project on this server.
- **Tenant** — ``<data_dir>/tenants/<tid>/.mini_cc/``. Owned by the
  tenant admin; applies to every project under that tenant.
- **Project** — ``<workspace>/.mini_cc/``. Owned by the project owner.

Discovery walks tiers system → tenant → project; same-named entries in
later tiers override earlier ones (project > tenant > system).
"""
from __future__ import annotations

import re
from enum import IntEnum
from pathlib import Path


class PluginTier(IntEnum):
    SYSTEM = 0
    TENANT = 1
    PROJECT = 2


_TID_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


def tier_dir(root: Path, tier: PluginTier,
             *, tenant_id: str | None = None) -> Path:
    """Return the ``.mini_cc/`` directory for the given tier.

    For ``PluginTier.SYSTEM`` ``root`` is the data dir. For ``TENANT``
    it's the data dir and ``tenant_id`` is required. For ``PROJECT`` it's
    the project workspace (already tenant-scoped by the caller).
    """
    root = Path(root)
    if tier is PluginTier.SYSTEM:
        return root / ".mini_cc"
    if tier is PluginTier.TENANT:
        if not tenant_id:
            raise ValueError("tenant_id is required for PluginTier.TENANT")
        if not _TID_SAFE.match(tenant_id):
            raise ValueError(f"unsafe tenant_id: {tenant_id!r}")
        return root / "tenants" / tenant_id / ".mini_cc"
    # PROJECT
    return root / ".mini_cc"


def ensure_tier_dir(root: Path, tier: PluginTier,
                    *, tenant_id: str | None = None) -> Path:
    """Materialize the tier's ``.mini_cc/`` (and ``skills/`` subdir).

    Idempotent. Returns the ``.mini_cc/`` path. Refuses unsafe
    tenant_ids so a malformed id can't escape the ``tenants/`` tree.
    """
    td = tier_dir(root, tier, tenant_id=tenant_id)
    (td / "skills").mkdir(parents=True, exist_ok=True)
    return td


def project_tier_dirs(data_dir: Path, tenant_id: str,
                      workspace: Path) -> list[Path]:
    """Return the canonical tier dir list for assembling a project's
    SkillLoader / mcp pool. Order matters: system first, project last
    so project wins on conflicts."""
    return [
        tier_dir(data_dir, PluginTier.SYSTEM),
        tier_dir(data_dir, PluginTier.TENANT, tenant_id=tenant_id),
        tier_dir(workspace, PluginTier.PROJECT),
    ]
