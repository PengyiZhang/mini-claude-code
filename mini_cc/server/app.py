"""FastAPI app factory + lifespan management."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..auth import TenantKeyRegistry
from ..projects import ProjectManager
from ..session import SessionManager
from .errors import MiniCCError, envelope, map_sdk_exception
from .routes import projects as projects_routes
from .routes import resources as resources_routes
from .routes import sessions as sessions_routes


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
              cors_origins: list[str] | None = None) -> FastAPI:
    """Wire a FastAPI app over the given SDK managers."""
    app = FastAPI(
        title="mini_cc",
        version="0.1.0",
        description="Multi-tenant agent framework HTTP/SSE transport.",
        lifespan=lifespan,
    )

    app.state.key_registry = key_registry
    app.state.pm = pm
    app.state.sm = sm

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
            expose_headers=["*"],
        )

    # Error handlers — render every MiniCCError through the standard envelope.
    @app.exception_handler(MiniCCError)
    async def _handle_mini_cc(request: Request, exc: MiniCCError):
        return JSONResponse(status_code=exc.status_code, content=envelope(exc))

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        mapped = map_sdk_exception(exc)
        return JSONResponse(status_code=mapped.status_code,
                            content=envelope(mapped))

    app.include_router(projects_routes.router)
    app.include_router(sessions_routes.router)
    app.include_router(resources_routes.router)
    app.include_router(resources_routes.download_router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"ok": True}

    return app
