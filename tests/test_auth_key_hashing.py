"""M2-2: hashed key storage — plaintext API keys never at rest.

keys.json stores entries under ``sha256:<64hex>`` names with a short
``key_hint`` prefix for identification. Legacy plaintext-named entries
keep authenticating and are re-hashed on the next mutation.
M2-7: rotation grace period downgrades the old key to read-only scopes
(never wider than the original).
"""
from __future__ import annotations

import json

import pytest

from mini_cc.auth.keys import TenantKeyRegistry
from mini_cc.auth.scope import scope_allows


def _registry(tmp_path) -> TenantKeyRegistry:
    return TenantKeyRegistry(tmp_path / "keys.json")


def _file_text(reg: TenantKeyRegistry) -> str:
    return reg.path.read_text(encoding="utf-8")


def _file_json(reg: TenantKeyRegistry) -> dict:
    return json.loads(_file_text(reg))


# ── M2-2: hashing at rest ───────────────────────────────────────────

def test_generated_key_stored_hashed_not_plaintext(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1")
    text = _file_text(reg)
    assert rec.key not in text, "plaintext key must never appear in keys.json"
    assert any(k.startswith("sha256:") for k in _file_json(reg))


def test_lookup_resolves_hashed_key_with_hint(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1", label="dev")
    got = reg.lookup(rec.key)
    assert got is not None
    assert got.tenant_id == "t1"
    assert got.label == "dev"
    assert got.key_hint and rec.key.startswith(got.key_hint)


def test_legacy_plaintext_entries_migrate_on_next_write(tmp_path):
    reg = _registry(tmp_path)
    legacy = "mck_" + "a" * 32
    reg._write({legacy: {"tenant_id": "t0", "scopes": ["*"],
                         "created_at": "", "label": "legacy"}})
    assert reg.lookup(legacy) is not None, "legacy entry must keep working"
    reg.generate("t1")  # any mutation rewrites the file
    assert legacy not in _file_text(reg), "legacy entry must get re-hashed"
    assert reg.lookup(legacy) is not None, "re-hashed entry still authenticates"


def test_revoke_update_rotate_accept_plaintext_after_hashing(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1")
    upd = reg.update(rec.key, label="renamed")
    assert upd is not None and upd.label == "renamed"
    new, _old = reg.rotate(rec.key, grace_hours=1)
    assert new.tenant_id == "t1"
    assert reg.lookup(new.key) is not None
    assert reg.revoke(new.key) is True
    assert reg.lookup(new.key) is None


def test_records_never_carry_plaintext_key_field(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1")
    for raw in _file_json(reg).values():
        assert not raw.get("key", "").startswith("mck_")


# ── M2-7: grace-period scope downgrade ──────────────────────────────

def test_grace_old_key_downgraded_to_read_only(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1", scopes=["*"])
    _new, old = reg.rotate(rec.key, grace_hours=2)
    assert old is not None
    assert scope_allows(old.scopes, "sessions", "GET") is True
    assert scope_allows(old.scopes, "sessions", "POST") is False
    assert scope_allows(old.scopes, "files", "DELETE") is False
    # downgrade survives a reload from disk
    reg2 = TenantKeyRegistry(reg.path)
    got = reg2.lookup(rec.key)
    assert got is not None
    assert scope_allows(got.scopes, "sessions", "POST") is False


def test_grace_downgrade_never_widens(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1", scopes=["sessions:read", "files:read"])
    _new, old = reg.rotate(rec.key, grace_hours=1)
    assert set(old.scopes) == {"sessions:read", "files:read"}
    assert scope_allows(old.scopes, "admin", "GET") is False


def test_hard_rotate_still_revokes_immediately(tmp_path):
    reg = _registry(tmp_path)
    rec = reg.generate("t1")
    new, old = reg.rotate(rec.key)  # no grace
    assert old is None
    assert reg.lookup(rec.key) is None
    assert reg.lookup(new.key) is not None
