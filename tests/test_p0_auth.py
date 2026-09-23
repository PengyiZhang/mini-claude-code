"""P0 tests for the tenant key registry (mini_cc.auth).

The registry backs the HTTP server's per-tenant auth and is
transport-agnostic: any future transport (WebSocket, gRPC) reuses it.

Phase D promoted keys from ``str → tenant_id`` to
``str → KeyRecord``. ``generate()`` returns a KeyRecord and
``lookup()`` returns ``KeyRecord | None`` (None on unknown OR expired).
"""
from __future__ import annotations

import threading

import pytest

from mini_cc.auth import TenantKeyRegistry


def test_generate_returns_mck_prefixed_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1")
    assert rec.key.startswith("mck_")
    assert len(rec.key) > len("mck_")
    assert rec.tenant_id == "tenant1"
    assert rec.scopes == ["*"]


def test_lookup_returns_tenant_for_valid_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1")
    found = reg.lookup(rec.key)
    assert found is not None
    assert found.tenant_id == "tenant1"


def test_lookup_returns_none_for_unknown_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    assert reg.lookup("mck_doesnotexist") is None
    assert reg.lookup("") is None


def test_revoke_makes_lookup_fail(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1")
    assert reg.revoke(rec.key) is True
    assert reg.lookup(rec.key) is None
    # Revoking again returns False
    assert reg.revoke(rec.key) is False


def test_list_for_returns_all_keys_for_tenant(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    r1 = reg.generate("tenant1")
    r2 = reg.generate("tenant1")
    r3 = reg.generate("tenant2")
    hints = [r.key_hint for r in reg.list_for("tenant1")]
    assert r1.key_hint in hints and r2.key_hint in hints
    assert r3.key_hint not in hints


def test_registry_persists_across_instances(tmp_path):
    path = tmp_path / "keys.json"
    reg1 = TenantKeyRegistry(path)
    rec = reg1.generate("tenant1")
    # New instance pointing at the same file must see the same key
    reg2 = TenantKeyRegistry(path)
    found = reg2.lookup(rec.key)
    assert found is not None
    assert found.tenant_id == "tenant1"


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
