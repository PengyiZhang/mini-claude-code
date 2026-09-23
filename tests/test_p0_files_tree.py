"""P0 regression: GET /files/tree hidden-file visibility controls.

Bug 2 from debug.6.md — the tree endpoint always hid dotfiles, which
meant system dirs like .mini_cc/ and .agents/ never showed up in the
UI. Fixed by adding an env var (MINI_CC_TREE_SHOW_HIDDEN) for the
operator default and a per-request ?include_hidden=true override.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}


@pytest.fixture
def tree_app(tmp_path, monkeypatch):
    # Default: hidden files hidden.
    monkeypatch.delenv("MINI_CC_TREE_SHOW_HIDDEN", raising=False)
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    import json as _json
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    client = TestClient(app)
    client.post("/tenants/tenant1/projects",
                headers=AUTH, json={"project_id": "p1"})
    # Seed both visible and hidden entries directly into the workspace.
    ws = pm.get("p1", tenant_id="tenant1").workspace
    (ws / "visible.txt").write_text("v", encoding="utf-8")
    (ws / ".hidden").mkdir()
    (ws / ".hidden" / "secret.txt").write_text("s", encoding="utf-8")
    (ws / ".mini_cc").mkdir(exist_ok=True)
    (ws / ".mini_cc" / "marker").write_text("m", encoding="utf-8")
    yield client


def test_tree_hides_dotfiles_by_default(tree_app):
    r = tree_app.get("/tenants/tenant1/projects/p1/files/tree",
                     headers=AUTH)
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert "visible.txt" in names
    assert ".hidden" not in names
    assert ".mini_cc" not in names


def test_tree_query_param_reveals_hidden(tree_app):
    r = tree_app.get("/tenants/tenant1/projects/p1/files/tree",
                     headers=AUTH, params={"include_hidden": "true"})
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert "visible.txt" in names
    assert ".hidden" in names
    assert ".mini_cc" in names


def test_tree_env_var_flips_default(tree_app, monkeypatch):
    """Operator default via env var — clients see hidden without
    passing a query param."""
    monkeypatch.setenv("MINI_CC_TREE_SHOW_HIDDEN", "1")
    r = tree_app.get("/tenants/tenant1/projects/p1/files/tree",
                     headers=AUTH)
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert ".mini_cc" in names
    assert ".hidden" in names


def test_tree_query_param_off_overrides_env_var(tree_app, monkeypatch):
    """Per-request ?include_hidden=false beats env var = on."""
    monkeypatch.setenv("MINI_CC_TREE_SHOW_HIDDEN", "true")
    r = tree_app.get("/tenants/tenant1/projects/p1/files/tree",
                     headers=AUTH, params={"include_hidden": "false"})
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert ".mini_cc" not in names
    assert "visible.txt" in names
