"""Migration script: pre-tenant-layout data → tenant-scoped layout."""
from __future__ import annotations

import json
from pathlib import Path

from mini_cc.projects.migrate_v0_tenant_layout import migrate


def _seed_legacy(data_dir: Path, tid_pid_pairs):
    """Write the OLD on-disk layout under data_dir/projects/<pid>/."""
    projects = data_dir / "projects"
    storage = data_dir / ".storage"
    for tid, pid in tid_pid_pairs:
        pd = projects / pid
        (pd / "workspace").mkdir(parents=True)
        (pd / "workspace" / "marker.txt").write_text(f"{tid}:{pid}")
        (pd / "meta.json").write_text(json.dumps({
            "project_id": pid,
            "tenant_id": tid,
            "created_at": "2026-01-01T00:00:00",
            "display_name": pid,
        }))
        # Legacy storage subdir.
        sp = storage / pid
        sp.mkdir(parents=True)
        (sp / "session.json").write_text("[]")


def test_dry_run_does_not_move(tmp_path):
    _seed_legacy(tmp_path, [("t1", "a"), ("t2", "b")])
    summary = migrate(tmp_path, apply=False)
    assert summary["migrated_projects"] == 2
    # Source files untouched.
    assert (tmp_path / "projects" / "a" / "meta.json").exists()
    assert (tmp_path / "projects" / "b" / "workspace" / "marker.txt").exists()
    # Targets not created.
    assert not (tmp_path / "tenants" / "t1").exists()


def test_apply_moves_projects_and_storage(tmp_path):
    _seed_legacy(tmp_path, [("t1", "a"), ("t2", "b")])
    summary = migrate(tmp_path, apply=True)
    assert summary["migrated_projects"] == 2
    assert summary["migrated_storage"] == 2

    # Project dir + workspace file moved.
    moved_ws = (tmp_path / "tenants" / "t1" / "projects" / "a"
                / "workspace" / "marker.txt")
    assert moved_ws.exists()
    assert moved_ws.read_text() == "t1:a"

    # Storage moved under new tenant root.
    moved_storage = (tmp_path / "tenants" / "t2" / ".storage"
                     / "b" / "session.json")
    assert moved_storage.exists()

    # Legacy dirs gone.
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / ".storage").exists()


def test_apply_is_idempotent(tmp_path):
    _seed_legacy(tmp_path, [("t1", "a")])
    migrate(tmp_path, apply=True)
    # Second run: no projects to move, no errors.
    summary = migrate(tmp_path, apply=True)
    assert summary["migrated_projects"] == 0
    assert summary["errors"] == []


def test_skips_unsafe_tenant_id(tmp_path):
    """Malformed tenant_id (path separators) must not be moved — it could
    escape the tenants/ tree."""
    projects = tmp_path / "projects"
    pd = projects / "x"
    pd.mkdir(parents=True)
    (pd / "meta.json").write_text(json.dumps({
        "project_id": "x",
        "tenant_id": "../escape",
        "created_at": "2026-01-01",
    }))
    summary = migrate(tmp_path, apply=True)
    assert summary["errors"]
    assert (tmp_path / "projects" / "x" / "meta.json").exists()


def test_missing_meta_skipped(tmp_path):
    projects = tmp_path / "projects"
    (projects / "leftover").mkdir(parents=True)  # no meta.json
    summary = migrate(tmp_path, apply=True)
    assert summary["migrated_projects"] == 0
    assert any("leftover" in s for s in summary["skipped"])


def test_no_legacy_dir_is_noop(tmp_path):
    summary = migrate(tmp_path, apply=True)
    assert summary["migrated_projects"] == 0
    assert summary["skipped"]
