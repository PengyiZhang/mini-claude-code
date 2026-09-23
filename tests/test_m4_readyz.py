"""M4-3: /readyz 探针——storage 门控、docker 降级非门控、TTL 缓存。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.sandbox.osdetect import DockerAvailability
from mini_cc.server import health as health_mod
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager


@pytest.fixture(autouse=True)
def _reset_caches():
    """health.py's TTL caches are module-global — production runs one app,
    but each test builds a fresh app with a different data_dir, so stale
    entries would leak across tests (e.g. a cached "unready" from a
    previous fixture). Reset before every test."""
    health_mod._docker_state.update(ts=0.0, available=None, reason="",
                                    version="")
    health_mod._readyz_cache.update(ts=0.0, payload=None)
    yield


@pytest.fixture
def client(monkeypatch, tmp_path):
    # /readyz reads LLM config lazily via mini_cc.config.default_config()
    # at request time (import inside the handler), so patching the module
    # attribute is seen by the endpoint. Same construction shape as
    # tests/test_p0_server.py's app fixture.
    monkeypatch.setattr("mini_cc.config.default_config",
                        lambda: SimpleNamespace(
                            has_llm_credentials=lambda: True))
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    return TestClient(app)


def _fake_docker(monkeypatch, available, reason=""):
    def probe(*, force=False):
        return DockerAvailability(available=available, reason=reason)
    monkeypatch.setattr("mini_cc.sandbox.probe_docker", probe)


def test_readyz_200_when_storage_and_llm_ok(client, monkeypatch):
    _fake_docker(monkeypatch, available=False, reason="no docker")
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["storage"]["ok"] is True
    assert body["checks"]["llm_configured"]["ok"] is True
    assert body["checks"]["docker"]["ok"] is False
    assert body["checks"]["docker"]["gating"] is False


def test_readyz_503_when_llm_unconfigured(client, monkeypatch):
    monkeypatch.setattr("mini_cc.config.default_config",
                        lambda: SimpleNamespace(
                            has_llm_credentials=lambda: False))
    _fake_docker(monkeypatch, available=True)
    assert client.get("/readyz").status_code == 503


def test_readyz_docker_gating_opt_in(client, monkeypatch):
    monkeypatch.setenv("MINI_CC_READYZ_REQUIRE_DOCKER", "1")
    _fake_docker(monkeypatch, available=False, reason="daemon down")
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["docker"]["gating"] is True


def test_readyz_result_cached(client, monkeypatch):
    """The readyz TTL cache means a second GET within 5s reuses the first
    payload — the docker probe (which spawns subprocesses in production)
    runs exactly once."""
    calls = []

    def probe(*, force=False):
        calls.append(force)
        return DockerAvailability(available=True)

    monkeypatch.setattr("mini_cc.sandbox.probe_docker", probe)
    client.get("/readyz")
    client.get("/readyz")
    assert len(calls) == 1
