"""Project.assets is wired in _assemble and persists across get() calls."""
from __future__ import annotations

from pathlib import Path

from mini_cc.projects import ProjectManager
from mini_cc.assets import AssetStore


_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
    "4e44ae426082"
)


def _png_bytes() -> bytes:
    return bytes.fromhex(_PNG_HEX)


def test_project_has_asset_store(tmp_path: Path):
    pm = ProjectManager(tmp_path / "projects")
    pm.create("tenant1", "p1")
    p = pm.get("p1", tenant_id="tenant1")
    assert isinstance(p.assets, AssetStore)
    # Round-trip: put + hydrate.
    png = _png_bytes()
    aid = p.assets.put(png, media_type="image/png", src="test")
    # Reload from cache drop — persistence.
    pm.invalidate("p1", tenant_id="tenant1")
    p2 = pm.get("p1", tenant_id="tenant1")
    assert p2.assets.get_meta(aid) is not None


def test_assets_dir_lands_in_project_dir(tmp_path: Path):
    """The .assets dir must live at <root>/tenants/<tid>/projects/<pid>/.assets
    (sibling of the workspace dir, scoped to one project)."""
    pm = ProjectManager(tmp_path / "projects")
    pm.create("tenant1", "p1")
    p = pm.get("p1", tenant_id="tenant1")
    expected = (tmp_path / "projects" / "tenants" / "tenant1"
                / "projects" / "p1" / ".assets")
    assert p.assets.root == expected
    assert expected.exists()


def test_assets_persisted_across_manager_instances(tmp_path: Path):
    """A brand-new ProjectManager reading the same data_dir picks up
    assets written by the previous instance — proves the AssetStore is
    reading from disk, not from a stale in-memory index."""
    root = tmp_path / "projects"
    pm1 = ProjectManager(root)
    pm1.create("tenant1", "p1")
    p1 = pm1.get("p1")
    aid = p1.assets.put(_png_bytes(), media_type="image/png", src="test")

    pm2 = ProjectManager(root)
    p2 = pm2.get("p1")
    assert p2.assets.get_meta(aid) is not None
    assert p2.assets.get_meta(aid)["bytes"] == len(_png_bytes())
