# Phase E: observability (metrics + tracing + token attribution)

## Context

Phases A–D are shipped (352 tests). Phase A laid the trace_id
groundwork: `TraceIdMiddleware` + JSON-formatted logging via
contextvars. Phase E builds on that to give operators what they need
to actually run mini_cc in production:

1. **Metrics** — `/metrics` (Prometheus) and `/metrics.json` (JSON
   snapshot). RED (Rate/Errors/Duration) histograms per route and
   per tenant, plus Anthropic token attribution.
2. **Tracing** — lightweight `log_span()` context manager as the
   default. OTel opt-in via `MINI_CC_OTEL_EXPORTER` for shops that
   already run Jaeger/Tempo.
3. **Token attribution** — per-tenant cumulative input/output/cache
   tokens from Anthropic `usage`, surfaced in both metric formats.

No auth on the metrics endpoints — they inherit the existing
trusted-network model (server binds 127.0.0.1 by default).

## Approach (locked via brainstorm)

- **Metric formats**: both Prometheus text at `/metrics` and JSON
  snapshot at `/metrics.json`. Full flexibility.
- **Metric catalog**: RED 4 (requests, errors, duration, in-flight)
  with per-tenant labels + token counters + Anthropic request
  counters/duration.
- **Tracing**: lightweight log spans by default. OTel opt-in via env.
  Zero hard OTel deps.
- **Tokens**: per-tenant cumulative (counters only — no per-session
  histograms to keep cardinality bounded).
- **Auth**: none on `/metrics`/`/metrics.json`; trusted-network model.

## File-by-file

### `mini_cc/server/metrics.py` (new, ~180 lines)

```python
class Counter:
    name: str
    help: str
    label_names: tuple[str, ...]
    _values: dict[tuple, float]
    _lock: threading.Lock

    def inc(self, value: float = 1.0, **labels) -> None: ...

class Histogram:
    name: str
    help: str
    label_names: tuple[str, ...]
    buckets: tuple[float, ...]  # seconds, e.g. (0.005, 0.01, ...)
    _counts: dict[tuple, list[int]]   # per-label, per-bucket
    _sums: dict[tuple, float]
    _totals: dict[tuple, int]
    _lock: threading.Lock

    def observe(self, value: float, **labels) -> None: ...

class Gauge:
    name: str
    help: str
    _value: int
    _lock: threading.Lock

    def inc(self, n: int = 1) -> None: ...
    def dec(self, n: int = 1) -> None: ...
    def set(self, n: int) -> None: ...

class MetricsRegistry:
    counters: dict[str, Counter]
    histograms: dict[str, Histogram]
    gauges: dict[str, Gauge]

    def counter(self, name, help, label_names) -> Counter: ...
    def histogram(self, name, help, label_names, buckets=DEFAULT) -> Histogram: ...
    def gauge(self, name, help) -> Gauge: ...

    def render_prometheus(self) -> str: ...
    def snapshot(self) -> dict: ...
```

Default buckets (seconds): 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
1.0, 2.5, 5.0, 10.0.

Built-in metrics (constructed in a `default_registry()` factory):

| Metric | Type | Labels |
| ------ | ---- | ------ |
| `http_requests_total` | counter | method, route_template, status, tenant |
| `http_request_duration_seconds` | histogram | method, route_template, tenant |
| `http_in_flight_requests` | gauge | — |
| `anthropic_tokens_total` | counter | tenant, kind |
| `anthropic_request_total` | counter | tenant, status |
| `anthropic_request_duration_seconds` | histogram | tenant |

`kind ∈ {input, output, cache_read, cache_create}`.
`status ∈ {success, error, cancelled}`.

Prometheus output rules:
- Each counter family: `# HELP <name> <help>`, `# TYPE <name> counter`,
  one `<name>{label="..."} <value>` line per label tuple.
- Each histogram: `# TYPE <name> histogram`,
  `<name>_bucket{...,le="0.005"} <count>` × buckets + `<name>_bucket{...,le="+Inf"} <total>`,
  `<name>_sum{...}`, `<name>_count{...}`.
- Gauge: `# TYPE <name> gauge`, `<name> <value>`.

### `mini_cc/server/tracing.py` (new, ~80 lines)

```python
@contextmanager
def log_span(name: str, **fields):
    start = time.perf_counter()
    tid = get_trace_id()
    tracer = _get_tracer()  # None if OTel disabled
    span = tracer.start_span(name) if tracer else None
    try:
        if span:
            ctx = trace_api.set_span_in_context(span)
            token = context.attach(ctx)
        yield
    except Exception as e:
        log.warning("span.end", extra={"span": name, "trace_id": tid,
                                       "error": type(e).__name__, **fields})
        if span:
            span.record_exception(e)
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR))
        raise
    finally:
        dur_ms = (time.perf_counter() - start) * 1000
        log.info("span.end", extra={"span": name, "trace_id": tid,
                                    "dur_ms": dur_ms, **fields})
        if span:
            span.set_attribute("dur_ms", dur_ms)
            for k, v in fields.items():
                span.set_attribute(k, v)
            span.end()
        if tracer:
            context.detach(token)
```

OTel init is lazy: `_get_tracer()` checks an env-cached flag. On first
call with `MINI_CC_OTEL_EXPORTER` set, imports
`opentelemetry.sdk.trace`, `opentelemetry.sdk.resources`, and the
appropriate exporter (`OTLPSpanExporter` for `otlp`,
`JaegerExporter` for `jaeger`, `ConsoleSpanExporter` for `console`).
Missing `opentelemetry-*` package → log warning + return None.

### `mini_cc/server/middleware.py` (modify — add MetricsMiddleware)

```python
class MetricsMiddleware:
    def __init__(self, app, registry: MetricsRegistry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not _enabled():
            return await self.app(scope, receive, send)

        method = scope["method"]
        # Resolve route template from the matched endpoint — this is
        # set by Starlette into scope["route"] after routing. We use a
        # wrapper that intercepts AFTER routing via the response.
        self.registry.gauges["http_in_flight_requests"].inc()
        start = time.perf_counter()
        status_code = {"v": 500}
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code["v"] = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur = time.perf_counter() - start
            self.registry.gauges["http_in_flight_requests"].dec()
            route_template = _extract_route_template(scope)
            tenant = scope.get("state", {}).get("tenant_id", "unknown")
            labels = {"method": method, "route_template": route_template,
                      "status": str(status_code["v"]), "tenant": tenant}
            self.registry.counters["http_requests_total"].inc(**labels)
            self.registry.histograms["http_request_duration_seconds"].observe(
                dur, method=method, route_template=route_template, tenant=tenant)
```

`_extract_route_template` reads from `scope["route"]` if set (Starlette
populates this on the scope after route matching), falling back to
`scope["path"]`. Templates give us `/tenants/{tid}/projects` instead
of `/tenants/acme/projects/p_123` — keeps cardinality bounded.

### `mini_cc/server/app.py` (modify)

- Wire `MetricsMiddleware` (outermost after TraceId).
- Add `/metrics` route returning `text/plain; version=0.0.4; charset=utf-8`.
- Add `/metrics.json` route returning JSON snapshot.

Order in `build_app()`:
1. TraceIdMiddleware (Phase A) — outermost.
2. MetricsMiddleware — observes lifecycle.
3. RateLimitMiddleware — innermost middleware (deps run after this).

### `mini_cc/server/cli.py` (modify)

Parse env vars into the registry init path:

| Env | Default | Purpose |
| --- | ------- | ------- |
| `MINI_CC_METRICS_ENABLED` | `1` | Master on/off for MetricsMiddleware |
| `MINI_CC_OTEL_EXPORTER` | (unset) | `otlp`, `jaeger`, `console`, or unset |
| `MINI_CC_OTEL_ENDPOINT` | `http://localhost:4317` | OTLP gRPC endpoint |
| `MINI_CC_OTEL_SERVICE_NAME` | `mini-cc` | Resource attribute |

Pass `MINI_CC_OTEL_*` into tracing's lazy init via module-level config.

### `mini_cc/core/loop.py` (modify)

In `run()`:
1. Wrap the `with self.client.messages.stream(...)` block in
   `log_span("anthropic.request", tenant=..., model=...)`.
2. After `stream.get_final_message()`, extract `response.usage` and
   call:
   ```python
   metrics.record_tokens(tenant_id,
       input=usage.input_tokens,
       output=usage.output_tokens,
       cache_read=getattr(usage, "cache_read_input_tokens", 0),
       cache_create=getattr(usage, "cache_creation_input_tokens", 0))
   ```
3. On `loop.stop()` cancellation path, increment
   `anthropic_request_total{status="cancelled"}` and skip token
   recording.
4. On exception, increment `anthropic_request_total{status="error"}`.

The metrics registry is pulled via `config.get_default_config()`
extension OR via a new optional `metrics` field on `ProjectRef`. The
latter is cleaner — `ProjectRef` already carries cross-cutting state
(`permissions`, `prompt_tools`).

### `tests/test_phaseE_metrics.py` (new)

- Counter: `inc(value=2)` accumulates; concurrent threads on same
  labels produce no lost updates.
- Histogram: bucket counts sum correctly; `_sum` tracks total;
  `_count` = N observations; `+Inf` bucket == total count.
- Gauge: inc/dec/set behave.
- `render_prometheus()`: well-formed output (`# HELP`, `# TYPE`, sorted
  label pairs, histogram emits `_bucket`/`_sum`/`_count`).
- `snapshot()`: dict shape matches contract (counters/histograms/gauges
  keys, scrape_ts present).

### `tests/test_phaseE_http.py` (new)

- TestClient: hit `/health` 5x; fetch `/metrics`; assert
  `http_requests_total{method="GET", route_template="/health",
  status="200", tenant="unknown"}` counter == 5.
- In-flight gauge returns to 0 between requests (assert via snapshot
  before/during/after).
- After authenticated call (`require_scope` sets tenant_id), the
  counter label uses the real tenant.

### `tests/test_phaseE_tracing.py` (new)

- `log_span("test")` emits a `span.end` INFO log line with `dur_ms`
  + `trace_id`.
- Inside `set_trace_id("abc")` context, the span inherits the trace.
- On exception, span.end is logged at WARNING with `error` field.
- OTel disabled (default) → `opentelemetry` not imported.

### `tests/test_phaseE_tokens.py` (new)

- Mocked Anthropic client returns `usage={input_tokens: 100,
  output_tokens: 50, cache_read_input_tokens: 200,
  cache_creation_input_tokens: 30}`.
- Drive `loop.run()` once; assert each `anthropic_tokens_total{kind=...}`
  counter incremented by the expected amount.
- Drive again; values accumulate.

### `mini_cc/README.md` + `.zh.md` (modify)

New "Observability" section:
- Metrics endpoints (`/metrics`, `/metrics.json`).
- Metric catalog table.
- Tracing model (log spans + OTel opt-in).
- Env vars.
- Quickstart with Prometheus + Grafana (scrape config snippet).

## Verification

```bash
# 1. Unit tests
python -m pytest tests/test_phaseE_*.py -v

# 2. Full suite
python -m pytest tests/ -q   # expect ~420 passing

# 3. Manual: Prometheus scrape
python -m mini_cc.server &
curl localhost:8000/metrics | head -40

# 4. Manual: JSON snapshot
curl localhost:8000/metrics.json | jq .

# 5. Manual: drive traffic, watch counters increment
for i in {1..5}; do curl -s localhost:8000/health >/dev/null; done
curl -s localhost:8000/metrics | grep http_requests_total

# 6. Manual: OTel (optional)
docker run -p 4317:4317 otel/opentelemetry-collector:latest
MINI_CC_OTEL_EXPORTER=otlp MINI_CC_OTEL_ENDPOINT=http://localhost:4317 \
  python -m mini_cc.server
# Drive some requests; spans land in collector logs.
```

## Out of scope for Phase E

- Grafana dashboard JSON templates (deferred to Phase F web UI work).
- Per-session token histograms (cardinality concern).
- `/metrics` auth (trusted-network model only).
- Custom application metrics from tools/hooks (Phase G).
- Distributed trace propagation across separate mini_cc instances
  (no upstream/downstream services today anyway).
