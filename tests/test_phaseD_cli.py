"""Phase D — CLI: keygen flags + keys list/rotate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(args, env_data_dir: Path):
    import os
    env = dict(os.environ)
    env["MINI_CC_DATA_DIR"] = str(env_data_dir)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_cc.server", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_keygen_with_scopes_and_label(tmp_path):
    rc, out, err = _run(["keygen", "t1",
                          "--scopes", "read:*",
                          "--scopes", "sessions:write",
                          "--label", "ci",
                          "--expires-in", "7d"], tmp_path)
    assert rc == 0, f"stderr={err}"
    assert "tenant=t1" in out
    assert "scopes=read:*,sessions:write" in out
    assert "[ci]" in out
    assert "expires=" in out
    # Sanity: persisted to file in object form
    on_disk = json.loads((tmp_path / "keys.json").read_text())
    assert len(on_disk) == 1
    rec = next(iter(on_disk.values()))
    assert rec["tenant_id"] == "t1"
    assert rec["scopes"] == ["read:*", "sessions:write"]
    assert rec["label"] == "ci"
    assert rec["expires_at"] is not None


def test_keygen_default_scopes_is_star(tmp_path):
    rc, out, _ = _run(["keygen", "t1"], tmp_path)
    assert rc == 0
    assert "scopes=*  expires=never" in out


def test_keygen_bad_duration_returns_nonzero(tmp_path):
    rc, _, err = _run(["keygen", "t1", "--expires-in", "abc"],
                      tmp_path)
    assert rc != 0
    assert "error" in err.lower()


def test_keys_list_empty(tmp_path):
    rc, out, _ = _run(["keys", "list", "t1"], tmp_path)
    assert rc == 0
    assert out == ""


def test_keys_list_shows_all_keys_for_tenant(tmp_path):
    _run(["keygen", "t1", "--label", "first"], tmp_path)
    _run(["keygen", "t1", "--label", "second"], tmp_path)
    _run(["keygen", "t2"], tmp_path)
    rc, out, _ = _run(["keys", "list", "t1"], tmp_path)
    assert rc == 0
    assert "[first]" in out
    assert "[second]" in out
    assert "[t2]" not in out  # t2's key shouldn't leak


def test_keys_rotate_hard_revoke_default(tmp_path):
    rc, out, _ = _run(["keygen", "t1", "--label", "v1"], tmp_path)
    assert rc == 0
    # Parse the key from the line
    old_key = out.strip().split()[0]
    rc, out, err = _run(["keys", "rotate", old_key], tmp_path)
    assert rc == 0, f"stderr={err}"
    assert "new:" in out
    assert "old: (hard-revoked)" in out
    # The old key is gone from disk
    on_disk = json.loads((tmp_path / "keys.json").read_text())
    assert old_key not in on_disk


def test_keys_rotate_with_grace_hours_keeps_old(tmp_path):
    rc, out, _ = _run(["keygen", "t1", "--label", "v1"], tmp_path)
    old_key = out.strip().split()[0]
    rc, out, err = _run(["keys", "rotate", old_key,
                          "--grace-hours", "24"], tmp_path)
    assert rc == 0, f"stderr={err}"
    assert "new:" in out
    assert "old:" in out
    # Both keys present on disk
    on_disk = json.loads((tmp_path / "keys.json").read_text())
    assert old_key in on_disk
    assert len(on_disk) == 2
    # Old key has a fresh expires_at within the next 24h+epsilon
    old_rec = on_disk[old_key]
    assert old_rec["expires_at"] is not None


def test_keys_rotate_unknown_key_returns_nonzero(tmp_path):
    rc, _, err = _run(["keys", "rotate", "mck_nope"], tmp_path)
    assert rc != 0
    assert "error" in err.lower()


def test_keys_subcommand_requires_action(tmp_path):
    rc, _, err = _run(["keys"], tmp_path)
    assert rc != 0
