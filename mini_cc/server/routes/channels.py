"""Channel management + inbound webhook routes.

Project-scoped CRUD mirrors the existing ``webhooks`` router shape
(tenant auth required). The inbound webhook endpoint is public — it has
no tenant auth, because external services (Feishu, Slack, …) call it
during their event subscription flow without our API keys. It resolves
the binding by globally-unique ``channel_id`` instead, validates the
binding's per-kind signature, parses the payload, and injects the
resulting user_input into the bound session via SessionManager.

Inbound routing
---------------
The route iterates ``pm.list()`` to find the binding. Channel ids are
globally unique (``chan_<12hex>``) so the lookup is unambiguous, and
project count is small enough (low double digits per tenant) that the
linear scan is cheaper than maintaining a global id → project index.

After resolving the binding:
1. Build the Channel instance from the binding's config.
2. Parse + verify the inbound body via ``Channel.handle_inbound``.
3. If it's a verification handshake → respond with the echoed body.
4. If it's a real user message → inject into the bound session.

Injection
---------
We DON'T call ``sm.send()`` inline because that blocks the HTTP worker
on the LLM stream (Feishu will time out before the first token). Instead
we enqueue the turn on a background thread and respond 200 immediately,
which is what Feishu expects. The bound session's AgentLoop emits the
turn via the standard event stream; outbound delivery flows back through
the channel's ``deliver`` method via the ChannelDispatcher installed on
the session.
"""
from __future__ import annotations

import logging
import time
import threading
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..deps import get_pm, get_sm, require_scope, validate_id
from ..errors import BadRequest, NotFound, TooManyRequests, Unauthorized

log = logging.getLogger(__name__)

# In-flight channel-inbound workers (M3-5). Daemon threads by design —
# they must not block process exit — but lifespan shutdown drains them
# so a turn already talking to the LLM gets its sink writes flushed
# instead of being axed mid-flight.
_INBOUND_WORKERS: set = set()
_WORKERS_LOCK = threading.Lock()


def drain_inbound_workers(timeout: float = 10.0) -> None:
    """Join in-flight channel-inbound workers (bounded by ``timeout``).

    Called from lifespan shutdown after teammates/MCP teardown. Workers
    that overrun the timeout stay daemon-dropped, as before."""
    with _WORKERS_LOCK:
        workers = [w for w in _INBOUND_WORKERS if w.is_alive()]
    deadline = time.monotonic() + timeout
    for w in workers:
        remaining = max(0.0, deadline - time.monotonic())
        w.join(timeout=remaining)
    # Prune only finished workers — ones that outlived the timeout stay
    # tracked so a later drain can still join them.
    with _WORKERS_LOCK:
        for w in list(_INBOUND_WORKERS):
            if not w.is_alive():
                _INBOUND_WORKERS.discard(w)


# ── Router 1: project-scoped CRUD (tenant auth required) ───────────────

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/channels",
    tags=["channels"],
)


def _registry_for(pm, pid: str, tid: str):
    """Resolve the project's channel registry. P0-1: enforces the tenant
    boundary exactly like the webhooks helper."""
    try:
        project = pm.get(pid, tenant_id=tid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if project.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")
    reg = getattr(project, "channels", None)
    if reg is None:
        raise NotFound("channel storage unavailable")
    return reg, project


class CreateChannelRequest(BaseModel):
    kind: str = Field(..., description="Channel kind, e.g. 'feishu'.")
    config: dict = Field(default_factory=dict,
                         description="Per-kind config (see docs).")
    session_id: str | None = Field(
        default=None,
        description="Lead session to route inbound into. None = project default.")
    event_types: list[str] = Field(
        default_factory=list,
        description="Outbound filter; empty subscribes to all event types.")


class ChannelOut(BaseModel):
    id: str
    kind: str
    config: dict
    session_id: str | None
    event_types: list[str]
    created_at: str


def _to_out(b) -> ChannelOut:
    # Mask secrets in config before returning over the API. Channels store
    # credentials (Feishu app_secret, Slack signing_secret, …) — leaking
    # them on GET would be a security regression. Masked keys are the
    # known set across all implemented kinds; future kinds can add theirs.
    safe_config = _mask_secrets(b.config)
    return ChannelOut(id=b.id, kind=b.kind, config=safe_config,
                      session_id=b.session_id, event_types=list(b.event_types),
                      created_at=b.created_at)


_SECRET_KEYS = {"app_secret", "encrypt_key", "verification_token",
                "signing_secret", "bot_token", "access_token"}


def _mask_secrets(cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS and isinstance(v, str) and v:
            out[k] = "***" if len(v) <= 3 else v[:3] + "***"
        else:
            out[k] = v
    return out


@router.get("", response_model=list[ChannelOut])
def list_channels(pid: str = Path(...),
                  tid: str = Depends(require_scope("projects:read")),
                  pm=Depends(get_pm)) -> list[ChannelOut]:
    validate_id(pid)
    reg, _project = _registry_for(pm, pid, tid)
    return [_to_out(b) for b in reg.list()]


@router.post("", response_model=ChannelOut, status_code=201)
def create_channel(body: CreateChannelRequest,
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:write")),
                   pm=Depends(get_pm),
                   request: Request = None) -> ChannelOut:
    validate_id(pid)
    reg, _project = _registry_for(pm, pid, tid)
    # Validate kind early so an unknown kind returns 400 (not a silent
    # binding that can never build a channel instance).
    from ...channels import registered_kinds
    if body.kind not in registered_kinds():
        raise BadRequest(f"unknown channel kind: {body.kind}")
    if body.session_id is not None:
        validate_id(body.session_id)
    binding = reg.add(body.kind, body.config,
                      session_id=body.session_id,
                      event_types=body.event_types)
    idx = _index(request)
    if idx is not None:
        idx[binding.id] = (tid, pid)
    return _to_out(binding)


@router.delete("/{channel_id}", status_code=204)
def delete_channel(channel_id: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:write")),
                   pm=Depends(get_pm),
                   request: Request = None) -> None:
    validate_id(pid)
    reg, _project = _registry_for(pm, pid, tid)
    if not reg.remove(channel_id):
        raise NotFound(f"channel {channel_id} not found")
    _index(request).pop(channel_id, None)


# ── Router 2: public inbound webhook ────────────────────────────────────

inbound_router = APIRouter(
    prefix="/channels",
    tags=["channels"],
)


def _index(request) -> dict:
    """channel_id -> (tenant_id, project_id) cache. Maintained by
    create/delete; inbound misses fall back to one scan and repopulate."""
    return getattr(request.app.state, "channel_index", None) if request is not None else None


def _find_binding(pm, channel_id: str, index: dict | None = None):
    """Resolve ``channel_id`` to ``(binding, project)`` or raise NotFound.

    M2-6: consults the app-level index first (inbound webhooks used to
    linear-scan every tenant's projects on EVERY request); a stale
    entry falls through to the one-time scan, which repopulates it."""
    if index is not None and channel_id in index:
        tid, pid = index[channel_id]
        try:
            project = pm.get(pid, tenant_id=tid)
        except KeyError:
            project = None
        if project is not None:
            b = getattr(project, "channels", None)
            b = b.get(channel_id) if b is not None else None
            if b is not None:
                return b, project
        index.pop(channel_id, None)  # stale — rescan below
    for project in pm.list_all():
        reg = getattr(project, "channels", None)
        if reg is None:
            continue
        b = reg.get(channel_id)
        if b is not None:
            if index is not None:
                index[channel_id] = (project.meta.tenant_id,
                                     project.project_id)
            return b, project
    raise NotFound(f"channel {channel_id} not found")


@inbound_router.post("/{channel_id}/webhook")
async def inbound_webhook(channel_id: str = Path(...),
                          request: Request = None,
                          pm=Depends(get_pm),
                          sm=Depends(get_sm)) -> Any:
    """Public inbound endpoint for any channel kind. Resolves the binding,
    builds the channel, parses + verifies the payload, and either:

    * responds with the channel's verification handshake body (during
      webhook setup), or
    * enqueues a background turn on the bound session and responds 200.

    M2-6 hardening: per-channel rate limit (``MINI_CC_CHANNEL_RPM``,
    default 30/min) and a hard requirement that the channel has inbound
    verification material — a binding whose kind can't verify payloads
    (e.g. Feishu without encrypt_key/verification_token) gets 401
    instead of silently accepting forged messages that trigger paid
    LLM turns.
    """
    import math as _math
    limiter = getattr(request.app.state, "channel_limiter", None)
    if limiter is not None:
        allowed, retry_after = limiter.allow(f"chan:{channel_id}")
        if not allowed:
            retry_after_int = (max(1, _math.ceil(retry_after))
                               if _math.isfinite(retry_after) else 60)
            request.state.rate_limit_retry_after = retry_after_int
            raise TooManyRequests(
                "channel webhook rate limit exceeded",
                details={"code": "rate_limited",
                         "retry_after": retry_after_int})

    binding, project = _find_binding(pm, channel_id, _index(request))
    # Build the channel. This must succeed since the binding was created
    # through ``create_channel`` which validates the kind — but defensive
    # in case a future kind was unregistered between create and now.
    channel = project.channels.get_channel(binding)
    if channel is None:
        raise NotFound(f"channel kind '{binding.kind}' unavailable")
    if not getattr(channel, "verification_configured", False):
        raise Unauthorized(
            f"channel {channel_id} has no inbound verification material "
            f"configured; set encrypt_key or verification_token for kind "
            f"'{binding.kind}' before enabling the webhook")

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    result = channel.handle_inbound(body, headers)

    if result.verification_response is not None:
        return JSONResponse(content=result.verification_response, status_code=200)

    if result.user_input is None:
        return PlainTextResponse("", status_code=200)

    # Inject on a background thread so we don't block the webhook caller
    # on the LLM stream (Feishu times out after ~3s). The session's
    # dispatcher picks up outbound events via ChannelDispatcher.
    _enqueue_inbound_turn(sm, project, binding.session_id,
                          result.user_input, result.metadata)
    return PlainTextResponse("", status_code=200)


def _enqueue_inbound_turn(sm, project, session_id: str | None,
                          user_input: str, metadata: dict) -> threading.Thread:
    """Fire-and-forget background thread that runs ``sm.send`` against
    the bound session. Creates the session lazily if it doesn't exist
    so a fresh channel binding can be the user's first message into the
    project.

    The webhook already returned 200 by the time this runs, so a failed
    turn can't surface to the caller — but it must not vanish silently
    either (M3-3): failures are logged with project/session context so
    operators can trace lost inbound messages. Returns the worker
    thread so callers/tests can join if they care."""
    def _worker():
        try:
            sid = session_id or _resolve_default_session(project, sm)
            if sid is None:
                log.warning(
                    "channel inbound turn dropped: no session to route "
                    "into (project=%s)", project.project_id)
                return
            for _ev in sm.send(project.project_id, sid, user_input):
                # Events are persisted + dispatched by the session's
                # on_event wrapper; we just drain the generator so the
                # turn actually runs.
                pass
        except Exception:
            log.warning(
                "channel inbound turn failed: project=%s session=%s "
                "input=%r", project.project_id, session_id,
                user_input[:120], exc_info=True)
        finally:
            with _WORKERS_LOCK:
                _INBOUND_WORKERS.discard(threading.current_thread())

    t = threading.Thread(target=_worker, daemon=True,
                         name=f"channel-inbound:{project.project_id}")
    with _WORKERS_LOCK:
        _INBOUND_WORKERS.add(t)
    t.start()
    return t


def _resolve_default_session(project, sm) -> str | None:
    """Pick or create the project's default lead session for channel
    inbound. Heuristic: reuse the most-recently-warmed session if one
    exists; otherwise create a new one named ``chan`` so it's easy to
    spot in the UI as the channel-bound session.

    Future: let projects mark an explicit 'default' session via
    metadata; for now this matches what a user would do manually."""
    metas = sm.list(project.project_id)
    # Prefer a non-teammate lead session.
    lead_metas = [m for m in metas
                  if not m.session_id.startswith("teammate-")]
    if lead_metas:
        # Sort by created_at desc if available; fall back to first.
        lead_metas.sort(key=lambda m: getattr(m, "created_at", ""),
                        reverse=True)
        return lead_metas[0].session_id
    sess = sm.start_session(project.project_id, "chan")
    return sess.session_id
