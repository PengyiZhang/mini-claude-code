"""FastAPI app factory + lifespan management."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..auth import TenantKeyRegistry
from ..projects import ProjectManager
from ..session import SessionManager
from ..sharing.tokens import warn_if_default_secret as share_secret_in_dev
from ..sharing.webhooks import warn_if_default_webhook_secret
from .errors import MiniCCError, envelope, map_sdk_exception
from .metrics import MetricsRegistry, default_registry
from .middleware import MetricsMiddleware, TraceIdMiddleware
from .ratelimit import TenantRateLimiter
from .routes import projects as projects_routes
from .routes import resources as resources_routes
from .routes import sessions as sessions_routes
from .routes import team as team_routes
from .routes import permissions as permissions_routes
from .routes import admin as admin_routes
from .routes import channels as channels_routes
from .routes import commands as commands_routes
from .routes import run_table as run_table_routes
from .routes import webhooks as webhooks_routes
from .routes import workflow_v2 as workflow_v2_routes


log = logging.getLogger("mini_cc.server.startup")


def _warn_insecure_defaults() -> None:
    """P0-C: surface dev-fallback secrets at startup so operators don't
    accidentally run a multi-tenant deployment where share tokens are
    signed with a process-random value (invalidating every link on
    restart) and webhook payloads share that same key. The HTTP API
    exposes `default_secret_in_use` per-call, but a startup log line is
    what actually gets noticed."""
    if share_secret_in_dev():
        log.warning(
            "MINI_CC_SHARE_SECRET not set — using dev-fallback secret. "
            "Share tokens will invalidate on every restart. "
            "DO NOT run multi-tenant production this way.")
    if warn_if_default_webhook_secret():
        log.warning(
            "MINI_CC_WEBHOOK_SECRET not set — using dev-fallback secret. "
            "Webhook signatures will invalidate on every restart. "
            "DO NOT run multi-tenant production this way.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _warn_insecure_defaults()
    # Importing the Feishu channel module runs its ``register_channel_kind``
    # side-effect so the registry knows about "feishu" bindings. Lazy
    # import so unrelated code paths don't pay the (small) startup cost.
    try:
        from ..channels import _ensure_feishu_loaded
        _ensure_feishu_loaded()
    except Exception:
        log.warning("Failed to register Feishu channel kind", exc_info=True)
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
    # B6: gracefully shutdown teammates so mailbox writes aren't mid-
    # flight when the process exits. Each TeammateSpawner sends a
    # shutdown_request via the bus and joins the worker threads.
    # Phase I.B-1.3: also stop each spawner's LeadWatcher so daemon
    # threads don't outlive the storage they're writing to.
    pm: ProjectManager = app.state.pm
    for pid in list(pm._projects.keys()) if hasattr(pm, "_projects") else []:
        try:
            project = pm.get(pid)
            spawner = getattr(project, "teams", None)
            if spawner is not None and hasattr(spawner, "shutdown"):
                spawner.shutdown(timeout=5.0)
            if spawner is not None and hasattr(spawner, "stop_lead_watcher"):
                spawner.stop_lead_watcher()
        except Exception:
            pass
    # A3: disconnect MCP clients so stdio subprocesses / HTTP pools
    # don't leak across restarts.
    for pid in list(pm._projects.keys()) if hasattr(pm, "_projects") else []:
        try:
            project = pm.get(pid)
            pool = getattr(project, "mcp_pool", None)
            if pool is not None and hasattr(pool, "disconnect_all"):
                pool.disconnect_all()
        except Exception:
            pass
    # Stop every per-tenant container the runtime context owns.
    ctx = getattr(app.state, "server_runtime", None)
    if ctx is not None:
        ctx.shutdown()


def build_app(*, data_dir: Path,
              key_registry: TenantKeyRegistry,
              pm: ProjectManager,
              sm: SessionManager,
              cors_origins: list[str] | None = None,
              rate_limiter: TenantRateLimiter | None = None,
              metrics_registry: MetricsRegistry | None = None,
              server_runtime: "object | None" = None) -> FastAPI:
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
    app.state.server_runtime = server_runtime

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
    app.include_router(team_routes.router)
    app.include_router(resources_routes.router)
    app.include_router(resources_routes.download_router)
    app.include_router(permissions_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(commands_routes.router)
    app.include_router(run_table_routes.router)
    app.include_router(sessions_routes.share_router)
    app.include_router(webhooks_routes.router)
    app.include_router(channels_routes.router)
    app.include_router(channels_routes.inbound_router)
    # Workflow V2 — definitions + runs (W1).
    for r in workflow_v2_routes.ALL_ROUTERS:
        app.include_router(r)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        # P0-3: surface LLM credential state so readiness probes can
        # distinguish "process alive" from "process can serve traffic".
        # A static {ok:true} hid every "started but unusable" config.
        from ..config import default_config
        cfg = default_config()
        configured = bool(getattr(cfg, "has_llm_credentials", lambda: False)())
        return {"ok": True, "llm_configured": configured}

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

    # ── Static frontend mount (optional) ───────────────────────────────
    # When the React UI has been built (mini_cc/web/dist), serve it from
    # the same FastAPI app so production deployments need only one origin
    # (no CORS). Detection order:
    #   1. MINI_CC_WEB_DIST env (absolute or relative path)
    #   2. <cwd>/mini_cc/web/dist       (running from repo root)
    #   3. <package>/web/dist           (running from installed package)
    # We mount /assets (etc.) at /assets for direct access, then add a
    # catch-all GET handler that serves index.html for unknown paths so
    # client-side routing (e.g. /projects/xyz) works. API routes are
    # registered above, so they take precedence over the catch-all.
    web_dist_env = os.getenv("MINI_CC_WEB_DIST")
    dist_candidates: list[Path] = []
    if web_dist_env:
        dist_candidates.append(Path(web_dist_env))
    dist_candidates.extend([
        Path.cwd() / "mini_cc" / "web" / "dist",
        Path(__file__).resolve().parent.parent / "web" / "dist",
    ])
    web_dist_path: Path | None = None
    for cand in dist_candidates:
        try:
            cand = cand.resolve()
        except (OSError, RuntimeError):
            continue
        if (cand / "index.html").exists():
            web_dist_path = cand
            break
    if web_dist_path is not None:
        from fastapi.responses import FileResponse

        app.state.web_dist = str(web_dist_path)

        # Direct static files (JS bundles, CSS, images). 404s here fall
        # through to the catch-all below.
        app.mount("/assets", StaticFiles(directory=str(web_dist_path / "assets")),
                  name="web-assets")

        async def _spa_fallback(_request: Request) -> FileResponse:
            """Serve index.html for any unknown path.

            Client-side routing (HashRouter here, but kept generic)
            needs this to support direct-loads of deep links. API
            routes and /assets/* are registered before this handler
            and take precedence."""
            return FileResponse(str(web_dist_path / "index.html"))

        # Catch-all GET — must come after all real routes.
        app.add_route("/{path:path}", _spa_fallback, methods=["GET"])
    else:
        app.state.web_dist = None

    return app
