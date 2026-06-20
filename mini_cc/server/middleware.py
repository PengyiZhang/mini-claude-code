"""Starlette middlewares: request-scoped trace_id + tenant context.

TraceIdMiddleware generates (or accepts) an X-Trace-Id and stores it
in contextvars so JSON log lines pick it up automatically.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import get_trace_id, new_trace_id, set_trace_id, set_tenant

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


__all__ = ["TraceIdMiddleware", "get_trace_id"]
