"""Phase E — MetricsRegistry primitives + Prometheus/JSON renderers."""
from __future__ import annotations

import threading

import pytest

from mini_cc.server.metrics import (Counter, Gauge, Histogram,
                                     MetricsRegistry, default_registry,
                                     record_tokens)


def test_counter_inc_accumulates():
    c = Counter("c", "help", ("method",))
    c.inc(method="GET")
    c.inc(2.5, method="GET")
    c.inc(method="POST")
    assert c.value(method="GET") == 3.5
    assert c.value(method="POST") == 1.0
    assert c.value(method="DELETE") == 0.0


def test_counter_rejects_negative():
    c = Counter("c", "help", ())
    with pytest.raises(ValueError):
        c.inc(-1)


def test_counter_concurrent_threads_no_lost_updates():
    c = Counter("c", "help", ("k",))

    def worker():
        for _ in range(1000):
            c.inc(k="x")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value(k="x") == 8000


def test_histogram_observe_buckets_cumulative():
    h = Histogram("h", "help", (), buckets=(0.1, 0.5, 1.0))
    for v in (0.05, 0.2, 0.7, 1.5):
        h.observe(v)
    # Each series: (key, bucket_counts, sum, total_count)
    series = h.items()
    assert len(series) == 1
    _, counts, total_sum, total_count = series[0]
    assert counts == [1, 2, 3]  # cumulative: ≤0.1→1, ≤0.5→2, ≤1.0→3
    assert total_count == 4
    assert total_sum == pytest.approx(0.05 + 0.2 + 0.7 + 1.5)


def test_histogram_per_label_isolated():
    h = Histogram("h", "help", ("route",), buckets=(1.0,))
    h.observe(0.5, route="/a")
    h.observe(0.5, route="/b")
    items = h.items()
    routes = {key[0]: total for key, _, _, total in items}
    assert routes == {"/a": 1, "/b": 1}


def test_gauge_inc_dec_set():
    g = Gauge("g", "help")
    assert g.value == 0
    g.inc()
    g.inc(2)
    assert g.value == 3
    g.dec()
    assert g.value == 2
    g.set(42)
    assert g.value == 42


def test_registry_factory_returns_existing():
    reg = MetricsRegistry()
    c1 = reg.counter("foo", "h", ("a",))
    c2 = reg.counter("foo", "h", ("a",))
    assert c1 is c2


def test_render_prometheus_counter_format():
    reg = MetricsRegistry()
    c = reg.counter("foo_total", "Total foos.", ("method",))
    c.inc(method="GET")
    c.inc(method="GET")
    c.inc(method="POST")
    out = reg.render_prometheus()
    assert "# HELP foo_total Total foos." in out
    assert "# TYPE foo_total counter" in out
    assert 'foo_total{method="GET"} 2.0' in out
    assert 'foo_total{method="POST"} 1.0' in out


def test_render_prometheus_histogram_emits_bucket_sum_count():
    reg = MetricsRegistry()
    h = reg.histogram("dur_seconds", "Latency.", ("route",),
                       buckets=(0.1, 1.0))
    h.observe(0.05, route="/a")
    h.observe(0.5, route="/a")
    h.observe(2.0, route="/a")
    out = reg.render_prometheus()
    assert "# TYPE dur_seconds histogram" in out
    assert 'dur_seconds_bucket{route="/a",le="0.1"} 1' in out
    assert 'dur_seconds_bucket{route="/a",le="1.0"} 2' in out
    assert 'dur_seconds_bucket{route="/a",le="+Inf"} 3' in out
    assert 'dur_seconds_sum{route="/a"} 2.55' in out
    assert 'dur_seconds_count{route="/a"} 3' in out


def test_render_prometheus_gauge():
    reg = MetricsRegistry()
    g = reg.gauge("inflight", "In-flight requests.")
    g.set(7)
    out = reg.render_prometheus()
    assert "# TYPE inflight gauge" in out
    assert "inflight 7" in out


def test_render_prometheus_no_labels_histogram():
    """Histograms without label_names still emit le=... correctly."""
    reg = MetricsRegistry()
    h = reg.histogram("nolabel", "Help", (), buckets=(1.0,))
    h.observe(0.5)
    out = reg.render_prometheus()
    assert 'nolabel_bucket{le="1.0"} 1' in out
    assert 'nolabel_bucket{le="+Inf"} 1' in out
    assert "nolabel_sum 0.5" in out
    assert "nolabel_count 1" in out


def test_snapshot_shape():
    reg = MetricsRegistry()
    reg.counter("c", "h", ("k",)).inc(k="v")
    reg.gauge("g", "h").set(3)
    reg.histogram("hh", "h", (), buckets=(1.0,)).observe(0.5)
    snap = reg.snapshot()
    assert "scrape_ts" in snap
    assert snap["counters"]["c"]["series"] == [
        {"labels": {"k": "v"}, "value": 1.0}]
    assert snap["gauges"]["g"]["value"] == 3
    hh_series = snap["histograms"]["hh"]["series"][0]
    assert hh_series["count"] == 1
    assert hh_series["sum"] == 0.5
    assert hh_series["bucket_counts"] == [1]


def test_default_registry_has_builtin_metric_families():
    reg = default_registry()
    assert "http_requests_total" in reg.counters
    assert "http_request_duration_seconds" in reg.histograms
    assert "http_in_flight_requests" in reg.gauges
    assert "anthropic_tokens_total" in reg.counters
    assert "anthropic_request_total" in reg.counters
    assert "anthropic_request_duration_seconds" in reg.histograms


def test_record_tokens_helper_splits_into_kinds():
    reg = default_registry()
    record_tokens(reg, "t1",
                  input=100, output=50, cache_read=200, cache_create=30)
    assert reg.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="input") == 100
    assert reg.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="output") == 50
    assert reg.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="cache_read") == 200
    assert reg.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="cache_create") == 30
    # Skips zeros
    record_tokens(reg, "t2", input=10)
    assert reg.counters["anthropic_tokens_total"].value(
        tenant="t2", kind="output") == 0
