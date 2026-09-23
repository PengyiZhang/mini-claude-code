"""M2-3 (S6): container extra_mounts host-path hardening.

Rules at config-load time:
- Sensitive host prefixes are ALWAYS denied (/etc, /root, /home,
  /var/lib/docker, /proc, /sys, /dev, /boot, and the mini_cc data_dir).
- When an allow-list is configured (MINI_CC_EXTRA_MOUNTS_ALLOW, or the
  ``allow_hosts`` override), it is STRICT: hosts outside it raise.
- The allow-list can never whitelist a denied prefix.
"""
from __future__ import annotations

import pytest

from mini_cc.sandbox.config import load_tenant_config


def _write_sandbox_toml(tenants_dir, host):
    tid_dir = tenants_dir / "t1"
    tid_dir.mkdir(parents=True, exist_ok=True)
    (tid_dir / "sandbox.toml").write_text(
        "enabled = true\n"
        "[[extra_mounts]]\n"
        f"host = '{host}'\n"
        'container = "/cache"\n'
        'options = "ro"\n',
        encoding="utf-8")


def _tenants(tmp_path):
    t = tmp_path / "data" / "tenants"
    t.mkdir(parents=True)
    return t


DENIED = ["/etc", "/etc/ssl", "/root", "/home", "/home/alice",
          "/var/lib/docker", "/proc", "/sys", "/dev", "/boot"]


@pytest.mark.parametrize("host", DENIED)
def test_sensitive_host_prefixes_rejected(tmp_path, host):
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, host)
    with pytest.raises(ValueError, match="extra_mounts"):
        load_tenant_config("t1", tenants)


def test_data_dir_subtree_rejected(tmp_path):
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, str(tmp_path / "data" / "other"))
    with pytest.raises(ValueError, match="extra_mounts"):
        load_tenant_config("t1", tenants)


def test_strict_allowlist_rejects_outside(tmp_path):
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, "/srv/shared")
    with pytest.raises(ValueError, match="extra_mounts"):
        load_tenant_config("t1", tenants,
                           allow_hosts=["/opt/cache"])


def test_strict_allowlist_accepts_inside(tmp_path):
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, "/opt/cache/layer2")
    cfg = load_tenant_config("t1", tenants, allow_hosts=["/opt/cache"])
    assert cfg.extra_mounts[0].host == "/opt/cache/layer2"


def test_allowlist_cannot_whitelist_denied_prefix(tmp_path):
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, "/etc/secrets")
    with pytest.raises(ValueError, match="extra_mounts"):
        load_tenant_config("t1", tenants, allow_hosts=["/etc"])


def test_no_allowlist_keeps_permissive_non_denied(tmp_path):
    """Zero-config behavior: anything not in the deny set loads."""
    tenants = _tenants(tmp_path)
    _write_sandbox_toml(tenants, "/srv/shared")
    cfg = load_tenant_config("t1", tenants, allow_hosts=None)
    assert cfg.extra_mounts[0].host == "/srv/shared"
