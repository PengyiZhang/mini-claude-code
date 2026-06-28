"""F7.3 — embed iframe route + share-token route wiring."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from starlette.testclient import TestClient  # noqa: E402

from mini_cc.auth import TenantKeyRegistry  # noqa: E402
from mini_cc.projects import ProjectManager  # noqa: E402
from mini_cc.server.app import build_app  # noqa: E402
from mini_cc.server.ratelimit import TenantRateLimiter  # noqa: E402
from mini_cc.session import SessionManager  # noqa: E402
from mini_cc.sharing.tokens import issue_share_token  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("t1")
    (tmp_path / "keys.json").write_text('{"mck_key1": "t1"}')
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    limiter = TenantRateLimiter(default_rpm=100)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    rate_limiter=limiter)
    return TestClient(app)


def test_embed_rejects_bad_token(tmp_path: Path):
    with _client(tmp_path) as c:
        r = c.get("/shared/not-a-real-token/embed")
        assert r.status_code == 401


def test_embed_returns_html_with_csp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINI_CC_SHARE_SECRET", "dev-share-secret-x")
    token = issue_share_token("p_test", "s_test",
                              secret="dev-share-secret-x")
    with _client(tmp_path) as c:
        r = c.get("/shared/" + token + "/embed")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    # Token is injected into the page.
    assert token in r.text


def test_share_link_create_requires_auth(tmp_path: Path):
    with _client(tmp_path) as c:
        r = c.post("/tenants/t1/projects/p/sessions/s/share")
        # No bearer → 401.
        assert r.status_code in (401, 403)
