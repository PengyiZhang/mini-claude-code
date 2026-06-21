"""Span logging + optional OpenTelemetry exporter.

Default mode is *log spans*: each ``log_span(name)`` block emits one
structured log line at exit containing ``span``, ``dur_ms``,
``trace_id`` and any caller-supplied fields. Zero extra deps.

When ``MINI_CC_OTEL_EXPORTER`` is set, the same calls also emit OTel
spans via the SDK + the configured exporter (``otlp`` / ``jaeger`` /
``console``). Missing ``opentelemetry-*`` packages downgrade
gracefully back to log-only mode with a single warning.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager

from .logging_config import get_trace_id

log = logging.getLogger("mini_cc.trace")

_OTEL_EXPORTER = None  # cached config value
_OTEL_TRACER = None    # cached tracer (None = disabled / unavailable)
_OTEL_INITIALIZED = False


def _configure_otel() -> None:
    """Lazy-init the OTel tracer based on env. Sets _OTEL_TRACER."""
    global _OTEL_EXPORTER, _OTEL_TRACER, _OTEL_INITIALIZED
    if _OTEL_INITIALIZED:
        return
    _OTEL_INITIALIZED = True
    exporter = os.environ.get("MINI_CC_OTEL_EXPORTER", "").strip().lower()
    if not exporter:
        return  # log-only mode
    _OTEL_EXPORTER = exporter
    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning(
            "otel_exporter_requested_but_sdk_missing",
            extra={"exporter": exporter,
                   "hint": "pip install opentelemetry-sdk "
                           "opentelemetry-exporter-otlp"})
        return

    service_name = os.environ.get("MINI_CC_OTEL_SERVICE_NAME", "mini-cc")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    span_exporter = None
    if exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        span_exporter = ConsoleSpanExporter()
    elif exporter == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,)
        except ImportError:
            log.warning("otel_otlp_exporter_missing")
            return
        endpoint = os.environ.get(
            "MINI_CC_OTEL_ENDPOINT", "http://localhost:4317")
        span_exporter = OTLPSpanExporter(endpoint=endpoint)
    elif exporter == "jaeger":
        try:
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter
        except ImportError:
            log.warning("otel_jaeger_exporter_missing")
            return
        span_exporter = JaegerExporter()
    else:
        log.warning("otel_unknown_exporter", extra={"exporter": exporter})
        return

    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace_api.set_tracer_provider(provider)
    _OTEL_TRACER = trace_api.get_tracer("mini_cc")


@contextmanager
def log_span(name: str, **fields):
    """Emit a structured span.end log line on exit. If OTel is enabled,
    also opens + closes a real OTel span around the block."""
    _configure_otel()
    start = time.perf_counter()
    tid = get_trace_id()
    tracer = _OTEL_TRACER
    span = tracer.start_span(name) if tracer else None
    token = None
    if span is not None:
        from opentelemetry import context as otel_context, trace as trace_api
        ctx = trace_api.set_span_in_context(span)
        token = otel_context.attach(ctx)
        for k, v in fields.items():
            try:
                span.set_attribute(k, v)
            except Exception:
                pass
    try:
        yield
    except Exception as e:
        log.warning(
            "span.end",
            extra={"span": name, "trace_id": tid,
                   "error": type(e).__name__, **fields})
        if span is not None:
            try:
                span.record_exception(e)
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR))  # type: ignore[name-defined]
            except Exception:
                pass
        raise
    finally:
        dur_ms = (time.perf_counter() - start) * 1000
        log.info(
            "span.end",
            extra={"span": name, "trace_id": tid,
                   "dur_ms": dur_ms, **fields})
        if span is not None:
            try:
                span.set_attribute("dur_ms", dur_ms)
                span.end()
            except Exception:
                pass
        if token is not None:
            otel_context.detach(token)  # type: ignore[name-defined]


def is_otel_enabled() -> bool:
    """True iff OTel SDK loaded and a tracer is active."""
    _configure_otel()
    return _OTEL_TRACER is not None
