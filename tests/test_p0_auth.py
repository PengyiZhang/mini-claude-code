"""P0 tests for the tenant key registry (mini_cc.auth).

The registry backs the HTTP server's per-tenant auth and is
transport-agnostic: any future transport (WebSocket, gRPC) reuses it.
"""
from __future__ import annotations

import threading

import pytest

from mini_cc.auth import TenantKeyRegistry


def test_generate_returns_mck_prefixed_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    key = reg.generate("tenant1")
    assert key.startswith("mck_")
    assert len(key) > len("mck_")


def test_lookup_returns_tenant_for_valid_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    key = reg.generate("tenant1")
    assert reg.lookup(key) == "tenant1"


def test_lookup_returns_none_for_unknown_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    assert reg.lookup("mck_doesnotexist") is None
    assert reg.lookup("") is None


def test_revoke_makes_lookup_fail(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    key = reg.generate("tenant1")
    assert reg.revoke(key) is True
    assert reg.lookup(key) is None
    # Revoking again returns False
    assert reg.revoke(key) is False


def test_list_for_returns_all_keys_for_tenant(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    k1 = reg.generate("tenant1")
    k2 = reg.generate("tenant1")
    k3 = reg.generate("tenant2")
    keys = reg.list_for("tenant1")
    assert k1 in keys and k2 in keys
    assert k3 not in keys


def test_registry_persists_across_instances(tmp_path):
    path = tmp_path / "keys.json"
    reg1 = TenantKeyRegistry(path)
    key = reg1.generate("tenant1")
    # New instance pointing at the same file must see the same key
    reg2 = TenantKeyRegistry(path)
    assert reg2.lookup(key) == "tenant1"


def test_concurrent_generate_no_lost_keys(tmp_path):
    """10 threads each generating 5 keys for the same tenant — all 50
    must be visible after, and the JSON file must not be corrupted."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")

    def worker():
        for _ in range(5):
            reg.generate("tenant1")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reg.list_for("tenant1")) == 50
