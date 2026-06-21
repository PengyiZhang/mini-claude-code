"""Phase E — HTTP integration: MetricsMiddleware + /metrics + /metrics.json."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.server.metrics import default_registry
from mini_cc.session import SessionManager


@pytest.fixture
def metrics_app(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    rec = reg.generate("tenant1")
    metrics = default_registry()
    pm = ProjectManager(tmp_path / "projects", metrics=metrics)
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm,
                    metrics_registry=metrics)
    yield TestClient(app), metrics, rec.key


AUTH = lambda key: {"Authorization": f"Bearer {key}"}


def test_metrics_endpoint_returns_prometheus_text(metrics_app):
    client, _, _ = metrics_app
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Has the standard metric families
    body = r.text
    assert "# TYPE http_requests_total counter" in body
    assert "# TYPE http_request_duration_seconds histogram" in body
    assert "# TYPE http_in_flight_requests gauge" in body


def test_metrics_json_endpoint_returns_snapshot(metrics_app):
    client, _, _ = metrics_app
    r = client.get("/metrics.json")
    assert r.status_code == 200
    data = r.json()
    assert "scrape_ts" in data
    assert "counters" in data and "histograms" in data
    assert "anthropic_tokens_total" in data["counters"]


def test_http_counter_increments_per_request(metrics_app):
    client, metrics, _ = metrics_app
    for _ in range(5):
        client.get("/health")
    # /health has no {tid} path param → tenant="unknown"
    val = metrics.counters["http_requests_total"].value(
        method="GET", route_template="/health",
        status="200", tenant="unknown")
    assert val == 5


def test_http_histogram_records_observations(metrics_app):
    client, metrics, _ = metrics_app
    client.get("/health")
    series = metrics.histograms["http_request_duration_seconds"].items()
    # /health label appears at least once
    matching = [s for s in series if "/health" in s[0]]
    assert len(matching) == 1
    assert matching[0][3] == 1  # total count


def test_in_flight_gauge_returns_to_zero_after_request(metrics_app):
    client, metrics, _ = metrics_app
    # By the time we get here, the test client's request has completed.
    client.get("/health")
    client.get("/health")
    assert metrics.gauges["http_in_flight_requests"].value == 0


def test_authenticated_request_carries_real_tenant_label(metrics_app):
    client, metrics, key = metrics_app
    # Generate a project to drive an authenticated call
    r = client.post("/tenants/tenant1/projects",
                    headers=AUTH(key),
                    json={"project_id": "p1"})
    assert r.status_code == 201
    # The POST /tenants/{tid}/projects route_template carries tenant="tenant1"
    val = metrics.counters["http_requests_total"].value(
        method="POST", route_template="/tenants/{tid}/projects",
        status="201", tenant="tenant1")
    assert val == 1


def test_route_template_used_not_raw_path(metrics_app):
    """The metric label is /tenants/{tid}/projects, not
    /tenants/tenant1/projects — cardinality bounded."""
    client, metrics, key = metrics_app
    client.post("/tenants/tenant1/projects", headers=AUTH(key),
                json={"project_id": "p1"})
    client.post("/tenants/tenant1/projects", headers=AUTH(key),
                json={"project_id": "p2"})
    # Both POSTs land under the same template label
    val = metrics.counters["http_requests_total"].value(
        method="POST", route_template="/tenants/{tid}/projects",
        status="201", tenant="tenant1")
    assert val == 2
