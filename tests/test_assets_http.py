"""POST/GET /tenants/{tid}/projects/{pid}/sessions/{sid}/assets."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


AUTH = {"Authorization": "Bearer mck_testkey"}

PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
    "4e44ae426082"
)


@pytest.fixture
def client(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    (tmp_path / "keys.json").write_text(
        json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    pm.create("tenant1", "p1")
    sm = SessionManager(pm)
    c = TestClient(build_app(data_dir=tmp_path, key_registry=reg,
                             pm=pm, sm=sm))
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    c.post("/tenants/tenant1/projects/p1/sessions",
           headers=AUTH, json={"session_id": "sess-1"})
    return c


def test_post_assets_uploads_returns_201(client):
    png = bytes.fromhex(PNG_HEX)
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/sess-1/assets",
        headers=AUTH,
        files={"file": ("a.png", png, "image/png")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["asset_id"]
    assert body["media_type"] == "image/png"
    assert body["bytes"] == len(png)


def test_post_assets_rejects_oversize(client):
    big = b"\0" * (5 * 1024 * 1024 + 1)
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/sess-1/assets",
        headers=AUTH,
        files={"file": ("big.png", big, "image/png")},
    )
    assert r.status_code == 400


def test_post_assets_rejects_unsupported_type(client):
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/sess-1/assets",
        headers=AUTH,
        files={"file": ("a.bmp", b"BMxxx", "image/bmp")},
    )
    assert r.status_code == 400


def test_get_asset_returns_bytes_with_content_type(client):
    png = bytes.fromhex(PNG_HEX)
    r = client.post(
        "/tenants/tenant1/projects/p1/sessions/sess-1/assets",
        headers=AUTH,
        files={"file": ("a.png", png, "image/png")})
    aid = r.json()["asset_id"]
    r2 = client.get(
        f"/tenants/tenant1/projects/p1/sessions/sess-1/assets/{aid}",
        headers=AUTH)
    assert r2.status_code == 200
    assert r2.content == png
    assert r2.headers["content-type"].startswith("image/png")


def test_assets_cross_tenant_returns_404(client):
    r = client.get(
        "/tenants/tenant1/projects/nonexistent/sessions/sess-1/assets/x",
        headers=AUTH)
    assert r.status_code == 404
