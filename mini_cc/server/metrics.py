"""Thread-safe metrics registry with Prometheus + JSON renderers.

Three metric types: ``Counter`` (monotonically increasing),
``Histogram`` (fixed-bucket observation distribution), ``Gauge``
(current value). All mutations go through a per-metric
``threading.Lock`` so concurrent workers can safely record.

The Prometheus text format is the same one scrape targets expose at
``/metrics``. JSON is for ad-hoc introspection from the web UI /
admin scripts.

Cardinality note: label values should be low-cardinality (HTTP
method, route template, status, tenant id). Never use session_id or
request URL here — they'd explode the size of the ``_values`` dict.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

# Seconds. Tuned for HTTP p99 in the low-double-digit-ms range.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


def _labels_to_key(label_names: tuple[str, ...], labels: dict[str, str]) -> tuple:
    """Resolve **labels against label_names into a stable tuple key.

    Missing labels become the empty string; unknown labels are dropped.
    """
    return tuple(str(labels.get(n, "")) for n in label_names)


def _escape_label_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(label_names: tuple[str, ...], key: tuple) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{n}="{_escape_label_value(v)}"'
        for n, v in zip(label_names, key)
    )
    return "{" + pairs + "}"


class Counter:
    """Monotonically increasing counter, keyed by label tuple."""

    def __init__(self, name: str, help: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help = help
        self.label_names = label_names
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels) -> None:
        if value < 0:
            raise ValueError("counter must increase; got negative value")
        key = _labels_to_key(self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def value(self, **labels) -> float:
        key = _labels_to_key(self.label_names, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def items(self) -> list[tuple[tuple, float]]:
        with self._lock:
            return list(self._values.items())


class Histogram:
    """Fixed-bucket observation distribution.

    Tracks per-label: per-bucket cumulative count, total count, sum.
    """

    def __init__(self, name: str, help: str,
                 label_names: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = DEFAULT_BUCKETS):
        self.name = name
        self.help = help
        self.label_names = label_names
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple, list[int]] = {}
        self._sums: dict[tuple, float] = {}
        self._totals: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels) -> None:
        key = _labels_to_key(self.label_names, labels)
        with self._lock:
            counts = self._counts.setdefault(
                key, [0] * len(self.buckets))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def items(self) -> list[tuple[tuple, list[int], float, int]]:
        with self._lock:
            out = []
            for key, counts in self._counts.items():
                out.append((key, list(counts),
                            self._sums.get(key, 0.0),
                            self._totals.get(key, 0)))
            return out


class Gauge:
    """Snapshot integer gauge."""

    def __init__(self, name: str, help: str):
        self.name = name
        self.help = help
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self._value += n

    def dec(self, n: int = 1) -> None:
        with self._lock:
            self._value -= n

    def set(self, n: int) -> None:
        with self._lock:
            self._value = n

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class MetricsRegistry:
    """Container for the metric families exposed at /metrics."""

    def __init__(self):
        self.counters: dict[str, Counter] = {}
        self.histograms: dict[str, Histogram] = {}
        self.gauges: dict[str, Gauge] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help: str,
                label_names: tuple[str, ...] = ()) -> Counter:
        with self._lock:
            if name in self.counters:
                return self.counters[name]
            c = Counter(name, help, label_names)
            self.counters[name] = c
            return c

    def histogram(self, name: str, help: str,
                  label_names: tuple[str, ...] = (),
                  buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> Histogram:
        with self._lock:
            if name in self.histograms:
                return self.histograms[name]
            h = Histogram(name, help, label_names, buckets)
            self.histograms[name] = h
            return h

    def gauge(self, name: str, help: str) -> Gauge:
        with self._lock:
            if name in self.gauges:
                return self.gauges[name]
            g = Gauge(name, help)
            self.gauges[name] = g
            return g

    def render_prometheus(self) -> str:
        """Render the whole registry in Prometheus 0.0.4 text format."""
        lines: list[str] = []

        def label_str_with_extra(label_names: tuple[str, ...],
                                  key: tuple, extra: tuple[str, str]) -> str:
            """Format `{n="v",...,extra_name="extra_value"}` (or empty)."""
            pairs = [(n, v) for n, v in zip(label_names, key)]
            pairs.append(extra)
            return "{" + ",".join(
                f'{n}="{_escape_label_value(v)}"' for n, v in pairs) + "}"

        for name in sorted(self.counters):
            c = self.counters[name]
            lines.append(f"# HELP {name} {c.help}")
            lines.append(f"# TYPE {name} counter")
            for key, val in sorted(c.items()):
                lines.append(f"{name}{_format_labels(c.label_names, key)} {val}")
        for name in sorted(self.histograms):
            h = self.histograms[name]
            lines.append(f"# HELP {name} {h.help}")
            lines.append(f"# TYPE {name} histogram")
            for key, counts, total_sum, total_count in sorted(h.items()):
                for bucket, cnt in zip(h.buckets, counts):
                    lines.append(
                        f"{name}_bucket"
                        f"{label_str_with_extra(h.label_names, key, ('le', str(bucket)))} "
                        f"{cnt}"
                    )
                lines.append(
                    f"{name}_bucket"
                    f'{label_str_with_extra(h.label_names, key, ("le", "+Inf"))} '
                    f"{total_count}"
                )
                lines.append(
                    f"{name}_sum{_format_labels(h.label_names, key)} {total_sum}")
                lines.append(
                    f"{name}_count{_format_labels(h.label_names, key)} {total_count}")
        for name in sorted(self.gauges):
            g = self.gauges[name]
            lines.append(f"# HELP {name} {g.help}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {g.value}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict:
        """JSON-friendly dict of the current state."""
        return {
            "scrape_ts": datetime.now(timezone.utc)
                          .isoformat(timespec="milliseconds")
                          .replace("+00:00", "Z"),
            "counters": {
                name: {
                    "help": c.help,
                    "label_names": list(c.label_names),
                    "series": [
                        {"labels": dict(zip(c.label_names, k)), "value": v}
                        for k, v in c.items()
                    ],
                }
                for name, c in self.counters.items()
            },
            "histograms": {
                name: {
                    "help": h.help,
                    "label_names": list(h.label_names),
                    "buckets": list(h.buckets),
                    "series": [
                        {
                            "labels": dict(zip(h.label_names, k)),
                            "bucket_counts": list(cnts),
                            "sum": s,
                            "count": total,
                        }
                        for k, cnts, s, total in h.items()
                    ],
                }
                for name, h in self.histograms.items()
            },
            "gauges": {
                name: {"help": g.help, "value": g.value}
                for name, g in self.gauges.items()
            },
        }

    def snapshot_for_tenant(self, tenant: str) -> dict:
        """Same as snapshot(), but only the series whose ``tenant``
        label matches. Gauges (no labels) are passed through as-is."""
        snap = self.snapshot()
        for family in snap["counters"].values():
            if "tenant" in family["label_names"]:
                family["series"] = [
                    s for s in family["series"]
                    if s["labels"].get("tenant") == tenant
                ]
        for family in snap["histograms"].values():
            if "tenant" in family["label_names"]:
                family["series"] = [
                    s for s in family["series"]
                    if s["labels"].get("tenant") == tenant
                ]
        return snap


def default_registry() -> MetricsRegistry:
    """Factory: register the built-in mini_cc metric families."""
    reg = MetricsRegistry()
    reg.counter("http_requests_total",
                "Total HTTP requests by method/route/status/tenant.",
                ("method", "route_template", "status", "tenant"))
    reg.histogram("http_request_duration_seconds",
                  "HTTP request latency in seconds.",
                  ("method", "route_template", "tenant"))
    reg.gauge("http_in_flight_requests",
              "HTTP requests currently being served.")
    reg.counter("anthropic_tokens_total",
                "Tokens consumed per tenant per kind.",
                ("tenant", "kind"))
    reg.counter("anthropic_request_total",
                "Anthropic API requests per tenant per outcome.",
                ("tenant", "status"))
    reg.histogram("anthropic_request_duration_seconds",
                  "Anthropic API request latency in seconds.",
                  ("tenant",),
                  buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
                           10.0, 30.0, 60.0, 120.0))
    return reg


def record_tokens(reg: MetricsRegistry, tenant: str, *,
                  input: int = 0, output: int = 0,
                  cache_read: int = 0, cache_create: int = 0) -> None:
    """Convenience: record one Anthropic usage snapshot."""
    if input:
        reg.counters["anthropic_tokens_total"].inc(
            input, tenant=tenant, kind="input")
    if output:
        reg.counters["anthropic_tokens_total"].inc(
            output, tenant=tenant, kind="output")
    if cache_read:
        reg.counters["anthropic_tokens_total"].inc(
            cache_read, tenant=tenant, kind="cache_read")
    if cache_create:
        reg.counters["anthropic_tokens_total"].inc(
            cache_create, tenant=tenant, kind="cache_create")
