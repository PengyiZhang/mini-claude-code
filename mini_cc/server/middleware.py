"""Starlette middlewares: request-scoped trace_id + tenant context.

TraceIdMiddleware generates (or accepts) an X-Trace-Id and stores it
in contextvars so JSON log lines pick it up automatically.

MetricsMiddleware (Phase E) records RED metrics per HTTP request.
Runs inside TraceIdMiddleware so the trace_id is visible during
metric emission.
"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .logging_config import get_trace_id, new_trace_id, set_trace_id, set_tenant
from .metrics import MetricsRegistry

_TRACE_HEADER = "X-Trace-Id"


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Assign a trace_id to every request. Propagates inbound X-Trace-Id."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        inbound = request.headers.get(_TRACE_HEADER)
        trace_id = inbound or new_trace_id()
        tenant = request.path_params.get("tid")
        set_trace_id(trace_id)
        if tenant:
            set_tenant(str(tenant))
        try:
            response = await call_next(request)
        finally:
            # Reset after the request finishes so worker threads don't leak
            # context between requests.
            set_trace_id(None)
            set_tenant(None)
        response.headers[_TRACE_HEADER] = trace_id
        return response


class MetricsMiddleware:
    """ASGI middleware that records HTTP RED metrics.

    Implemented as a raw ASGI middleware (not BaseHTTPMiddleware) so
    it sees the routing decision in ``scope["route"]`` and gets
    accurate timing without the extra Request/Response allocation
    overhead BaseHTTPMiddleware adds.
    """

    def __init__(self, app: ASGIApp, registry: MetricsRegistry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method", "UNKNOWN")
        self.registry.gauges["http_in_flight_requests"].inc()
        start = time.perf_counter()
        status_box = {"v": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_box["v"] = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur = time.perf_counter() - start
            self.registry.gauges["http_in_flight_requests"].dec()
            route_template = _extract_route_template(scope)
            state = scope.get("state") or {}
            tenant = state.get("tenant_id") or "unknown"
            self.registry.counters["http_requests_total"].inc(
                method=method, route_template=route_template,
                status=str(status_box["v"]), tenant=tenant)
            self.registry.histograms["http_request_duration_seconds"].observe(
                dur, method=method,
                route_template=route_template, tenant=tenant)


def _extract_route_template(scope: Scope) -> str:
    """Pull the FastAPI/Starlette route template (e.g.
    `/tenants/{tid}/projects`) if available; fall back to raw path."""
    route = scope.get("route")
    if route is not None:
        path = getattr(route, "path", None)
        if path:
            return path
    return scope.get("path", "unknown")


__all__ = ["TraceIdMiddleware", "MetricsMiddleware", "get_trace_id"]
