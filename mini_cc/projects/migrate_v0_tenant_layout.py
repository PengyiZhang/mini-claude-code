"""One-shot migration: pre-tenant-layout data → tenant-scoped layout.

Before this change, on-disk layout was flat:

    <data>/
        projects/
            <pid>/
                workspace/
                .state/
                meta.json    (carries tenant_id)
        .storage/            (FSStorage root, shared across all projects)

After the change:

    <data>/
        tenants/
            <tid>/
                projects/
                    <pid>/
                        workspace/
                        .state/
                        meta.json
                .storage/
                    <pid>/   (per-project subdir, as before)

The migration reads each <pid>/meta.json, extracts tenant_id, and moves
both the project dir and the matching storage subdir to their new
tenant-scoped homes.

Usage::

    python -m mini_cc.projects.migrate_v0_tenant_layout <data_dir>
    python -m mini_cc.projects.migrate_v0_tenant_layout <data_dir> --apply

Without --apply the script runs in dry-run mode and prints what it
would do; nothing on disk is touched. Idempotent: re-running after
--apply is a no-op.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _read_meta(old_project_dir: Path) -> dict | None:
    fp = old_project_dir / "meta.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _safe_tenant_dir(tid: str) -> str:
    # Defensive: never let a malformed tenant_id escape the tenants/ tree.
    if not tid or any(c in tid for c in "/\\.."):
        raise ValueError(f"refusing unsafe tenant_id: {tid!r}")
    return tid


def migrate(data_dir: Path, *, apply: bool = False) -> dict:
    data_dir = Path(data_dir).resolve()
    old_projects = data_dir / "projects"
    old_storage = data_dir / ".storage"
    tenants_dir = data_dir / "tenants"

    summary: dict = {
        "data_dir": str(data_dir),
        "apply": apply,
        "migrated_projects": 0,
        "migrated_storage": 0,
        "skipped": [],
        "errors": [],
    }

    if not old_projects.exists():
        summary["skipped"].append(
            f"no legacy projects dir at {old_projects} — nothing to do")
        return summary

    for pid_dir in old_projects.iterdir():
        if not pid_dir.is_dir():
            continue
        meta = _read_meta(pid_dir)
        if meta is None:
            summary["skipped"].append(
                f"{pid_dir.name}: no readable meta.json, skipping")
            continue
        tid = meta.get("tenant_id")
        if not tid:
            summary["skipped"].append(
                f"{pid_dir.name}: meta has no tenant_id, skipping")
            continue
        try:
            _safe_tenant_dir(tid)
        except ValueError as e:
            summary["errors"].append(str(e))
            continue

        new_project_dir = tenants_dir / tid / "projects" / pid_dir.name
        new_storage_dir = tenants_dir / tid / ".storage" / pid_dir.name

        if new_project_dir.exists():
            summary["skipped"].append(
                f"{pid_dir.name}: target {new_project_dir} already exists, "
                "leaving legacy copy in place")
        else:
            if apply:
                new_project_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(pid_dir), str(new_project_dir))
            summary["migrated_projects"] += 1
            print(f"[{'MOVE' if apply else 'DRY-RUN'}] "
                  f"{pid_dir} → {new_project_dir}")

        # Storage migration: best-effort, optional.
        old_storage_pid = old_storage / pid_dir.name
        if old_storage_pid.exists():
            if new_storage_dir.exists():
                summary["skipped"].append(
                    f"{pid_dir.name}: storage target already exists, "
                    "leaving legacy copy")
            else:
                if apply:
                    new_storage_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_storage_pid), str(new_storage_dir))
                summary["migrated_storage"] += 1
                print(f"[{'MOVE' if apply else 'DRY-RUN'}] "
                      f"{old_storage_pid} → {new_storage_dir}")

    # Clean up empty legacy dirs (only when applying).
    if apply:
        for legacy in (old_projects, old_storage):
            try:
                if legacy.exists() and not any(legacy.iterdir()):
                    legacy.rmdir()
                    print(f"[CLEANUP] removed empty {legacy}")
            except OSError:
                pass

    return summary


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("data_dir", type=Path,
                   help="mini_cc data dir (containing legacy projects/)")
    p.add_argument("--apply", action="store_true",
                   help="actually move files; default is dry-run")
    args = p.parse_args(argv)
    if not args.data_dir.exists():
        print(f"error: {args.data_dir} does not exist", file=sys.stderr)
        return 2
    summary = migrate(args.data_dir, apply=args.apply)
    print()
    print(f"mode: {'APPLY' if summary['apply'] else 'DRY-RUN'}")
    print(f"projects moved: {summary['migrated_projects']}")
    print(f"storage dirs moved: {summary['migrated_storage']}")
    print(f"skipped: {len(summary['skipped'])}")
    print(f"errors: {len(summary['errors'])}")
    for e in summary["errors"]:
        print(f"  ERROR: {e}", file=sys.stderr)
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
