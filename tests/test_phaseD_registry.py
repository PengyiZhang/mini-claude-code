"""Phase D — TenantKeyRegistry: scopes, expiry, rotation, migration."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from mini_cc.auth import TenantKeyRegistry
from mini_cc.auth.keys import KeyRecord


def test_generate_with_defaults_has_star_scope_no_expiry(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1")
    assert rec.tenant_id == "t1"
    assert rec.scopes == ["*"]
    assert rec.expires_at is None
    assert rec.created_at != ""
    assert rec.label == ""
    assert rec.rotated_from is None


def test_generate_with_scopes_expiry_label(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate(
        "t1",
        scopes=["read:*", "sessions:write"],
        expires_in="7d",
        label="ci",
    )
    assert rec.scopes == ["read:*", "sessions:write"]
    assert rec.expires_at is not None
    assert rec.label == "ci"


def test_lookup_returns_record_with_full_fields(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", scopes=["sessions:read"], label="audit")
    found = reg.lookup(rec.key)
    assert found is not None
    assert found.tenant_id == "t1"
    assert found.scopes == ["sessions:read"]
    assert found.label == "audit"


def test_lookup_rejects_expired_key_lazily(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", expires_in=1)  # 1 second
    # Still valid immediately
    assert reg.lookup(rec.key) is not None
    # Manually expire by patching the on-disk record
    raw = reg._read_raw()
    raw[rec.key]["expires_at"] = "2000-01-01T00:00:00Z"
    reg._write(raw)
    assert reg.lookup(rec.key) is None


def test_expired_key_still_listed_for_admin(tmp_path):
    """Lazy expiry: lookup rejects but list_for still includes expired keys."""
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", expires_in=1)
    raw = reg._read_raw()
    raw[rec.key]["expires_at"] = "2000-01-01T00:00:00Z"
    reg._write(raw)
    listed = reg.list_for("t1")
    assert len(listed) == 1
    assert listed[0].key == rec.key


def test_list_for_filters_by_tenant(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("t1")
    reg.generate("t1")
    reg.generate("t2")
    assert len(reg.list_for("t1")) == 2
    assert len(reg.list_for("t2")) == 1
    assert len(reg.list_for("t3")) == 0


def test_revoke_removes_key(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1")
    assert reg.revoke(rec.key) is True
    assert reg.lookup(rec.key) is None
    assert reg.revoke(rec.key) is False


def test_rotate_hard_revoke_default(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", scopes=["sessions:read"])
    new_rec, old_rec = reg.rotate(rec.key)
    assert new_rec.tenant_id == "t1"
    assert new_rec.scopes == ["sessions:read"]  # inherited
    assert new_rec.rotated_from == rec.key
    assert old_rec is None  # hard-revoked
    # Old key no longer works
    assert reg.lookup(rec.key) is None
    # New key does
    assert reg.lookup(new_rec.key) is not None


def test_rotate_with_grace_hours_keeps_old(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", scopes=["sessions:write"])
    new_rec, old_rec = reg.rotate(rec.key, grace_hours=24)
    assert old_rec is not None
    # Both work
    assert reg.lookup(rec.key) is not None
    assert reg.lookup(new_rec.key) is not None
    # Old key has a new expires_at within the next 24h+epsilon
    assert old_rec.expires_at is not None
    # Inherit scopes on new key
    assert new_rec.scopes == ["sessions:write"]


def test_rotate_unknown_key_raises(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    with pytest.raises(KeyError):
        reg.rotate("mck_doesnotexist")


def test_rotate_overrides_scopes_and_label(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1", scopes=["read:*"])
    new_rec, _ = reg.rotate(
        rec.key,
        scopes=["sessions:read"],
        label="downgraded",
    )
    assert new_rec.scopes == ["sessions:read"]
    assert new_rec.label == "downgraded"


def test_update_patches_fields(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("t1")
    updated = reg.update(rec.key,
                         scopes=["read:*"],
                         label="read-only")
    assert updated is not None
    assert updated.scopes == ["read:*"]
    assert updated.label == "read-only"
    # Round-trip
    found = reg.lookup(rec.key)
    assert found is not None
    assert found.scopes == ["read:*"]
    assert found.label == "read-only"


def test_update_returns_none_for_unknown(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    assert reg.update("mck_nope", scopes=["x"]) is None


def test_migration_promotes_bare_string_format(tmp_path):
    """A keys.json written by the pre-Phase-D format (bare str values)
    must be readable as KeyRecord with scopes=['*'] and label='migrated'."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"mck_legacy": "t1"}))
    reg = TenantKeyRegistry(path)
    found = reg.lookup("mck_legacy")
    assert found is not None
    assert found.tenant_id == "t1"
    assert found.scopes == ["*"]
    assert found.label == "migrated"
    # File is NOT rewritten on read — stays byte-identical until mutation
    assert json.loads(path.read_text()) == {"mck_legacy": "t1"}


def test_migration_survives_after_mutation(tmp_path):
    """After a generate()/revoke()/etc, the legacy bare-string entry is
    promoted to the new object shape on disk."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"mck_legacy": "t1"}))
    reg = TenantKeyRegistry(path)
    reg.generate("t2")
    on_disk = json.loads(path.read_text())
    assert isinstance(on_disk["mck_legacy"], dict)
    assert on_disk["mck_legacy"]["tenant_id"] == "t1"
    assert on_disk["mck_legacy"]["scopes"] == ["*"]


def test_keyrecord_is_expired_tolerates_malformed():
    rec = KeyRecord(key="mck_x", tenant_id="t1",
                    scopes=["*"], created_at="", expires_at="garbage")
    assert rec.is_expired() is False  # fail-open on malformed


def test_keyrecord_is_expired_none_means_never():
    rec = KeyRecord(key="mck_x", tenant_id="t1",
                    scopes=["*"], created_at="", expires_at=None)
    assert rec.is_expired() is False


def test_keyrecord_is_expired_in_past():
    rec = KeyRecord(key="mck_x", tenant_id="t1",
                    scopes=["*"], created_at="",
                    expires_at="2000-01-01T00:00:00Z")
    assert rec.is_expired() is True


def test_keyrecord_is_expired_in_future():
    future = (datetime.now(timezone.utc) + timedelta(days=7))
    rec = KeyRecord(key="mck_x", tenant_id="t1",
                    scopes=["*"], created_at="",
                    expires_at=future.isoformat())
    assert rec.is_expired() is False
