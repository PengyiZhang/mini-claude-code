"""FastAPI app factory + lifespan management."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from ..auth import TenantKeyRegistry
from ..projects import ProjectManager
from ..session import SessionManager
from .errors import MiniCCError, envelope, map_sdk_exception
from .metrics import MetricsRegistry, default_registry
from .middleware import MetricsMiddleware, TraceIdMiddleware
from .ratelimit import TenantRateLimiter
from .routes import projects as projects_routes
from .routes import resources as resources_routes
from .routes import sessions as sessions_routes
from .routes import permissions as permissions_routes
from .routes import admin as admin_routes


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Shutdown: stop every live session. Sessions are in-memory so they
    # die with the process anyway, but we want clean loop.stop() flags
    # so the Anthropic client doesn't hang mid-call.
    sm: SessionManager = app.state.sm
    for (pid, sid), sess in list(sm._sessions.items()):
        try:
            sess.stop()
        except Exception:
            pass


def build_app(*, data_dir: Path,
              key_registry: TenantKeyRegistry,
              pm: ProjectManager,
              sm: SessionManager,
              cors_origins: list[str] | None = None,
              rate_limiter: TenantRateLimiter | None = None,
              metrics_registry: MetricsRegistry | None = None) -> FastAPI:
    """Wire a FastAPI app over the given SDK managers."""
    app = FastAPI(
        title="mini_cc",
        version="0.1.0",
        description="Multi-tenant agent framework HTTP/SSE transport.",
        lifespan=lifespan,
    )

    if metrics_registry is None:
        metrics_registry = default_registry()

    app.state.key_registry = key_registry
    app.state.pm = pm
    app.state.sm = sm
    app.state.rate_limiter = rate_limiter
    app.state.metrics = metrics_registry

    # TraceId is the outermost so every downstream log line (including
    # CORS rejections) carries a trace_id. MetricsMiddleware sits
    # inside it so the trace_id is visible during metric emission.
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(MetricsMiddleware, registry=metrics_registry)

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID",
                           "X-Trace-Id"],
            expose_headers=["X-Trace-Id"],
        )

    # Error handlers — render every MiniCCError through the standard envelope.
    @app.exception_handler(MiniCCError)
    async def _handle_mini_cc(request: Request, exc: MiniCCError):
        headers: dict[str, str] | None = None
        retry_after = getattr(request.state, "rate_limit_retry_after", None)
        if retry_after or exc.extra_headers:
            headers = {}
            if retry_after:
                headers["Retry-After"] = str(retry_after)
            headers.update(exc.extra_headers)
        return JSONResponse(status_code=exc.status_code,
                            content=envelope(exc),
                            headers=headers)

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        mapped = map_sdk_exception(exc)
        return JSONResponse(status_code=mapped.status_code,
                            content=envelope(mapped))

    app.include_router(projects_routes.router)
    app.include_router(sessions_routes.router)
    app.include_router(resources_routes.router)
    app.include_router(resources_routes.download_router)
    app.include_router(permissions_routes.router)
    app.include_router(admin_routes.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"ok": True}

    @app.get("/metrics", tags=["meta"])
    def metrics_text() -> PlainTextResponse:
        """Prometheus 0.0.4 text format. Trusted-network only — no auth."""
        reg: MetricsRegistry = app.state.metrics
        return PlainTextResponse(
            reg.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/metrics.json", tags=["meta"])
    def metrics_json() -> dict:
        """JSON snapshot for ad-hoc introspection. Trusted-network only."""
        reg: MetricsRegistry = app.state.metrics
        return reg.snapshot()

    return app
