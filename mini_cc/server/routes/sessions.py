"""Session routes: start / list / remove / resume / send (SSE)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import StreamingResponse

from ..deps import (check_rate_limit_scope, get_pm, get_sm, require_scope,
                    validate_id)
from ..errors import Conflict, NotFound, map_sdk_exception
from ..schemas import (CreateSessionRequest, SendMessageRequest, SessionMeta,
                       SessionOut)
from ..sse import sse_stream

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions",
    tags=["sessions"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    """Ensure the project exists and belongs to this tenant. Raises 404
    for missing or cross-tenant access."""
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


@router.post("", response_model=SessionOut)
def start_session(body: CreateSessionRequest,
                  response: Response,
                  pid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:write")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> SessionOut:
    """Idempotent: 201 if a new session was created, 200 if an existing
    on-disk session was re-warmed."""
    validate_id(pid)
    if body.session_id is not None:
        validate_id(body.session_id)
    _check_project_tenant(pid, tid, pm)

    project = pm.get(pid)
    existed = (body.session_id is not None
               and body.session_id in {m.session_id for m
                                       in project.storage.list_sessions(pid)})

    try:
        sess = sm.start_session(pid, body.session_id, model=body.model)
    except Exception as e:
        raise map_sdk_exception(e)
    response.status_code = 200 if existed else 201
    return SessionOut(project_id=pid, session_id=sess.session_id,
                      created=not existed)


@router.get("", response_model=list[SessionMeta])
def list_sessions(pid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:read")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> list[SessionMeta]:
    validate_id(pid)
    _check_project_tenant(pid, tid, pm)
    return [SessionMeta(**m.__dict__) for m in sm.list(pid)]


@router.post("/{sid}/resume", response_model=SessionMeta)
def resume_session(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("sessions:write")),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> SessionMeta:
    """Explicit warm-load of an on-disk session. 200 if warmed (idempotent
    if already in memory). 404 if not on disk."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    try:
        sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")
    # Re-read the meta so in_memory=True reflects the just-completed warm.
    metas = {m.session_id: m for m in sm.list(pid)}
    m = metas.get(sid)
    if m is None:
        raise NotFound(f"session {sid} not found")
    return SessionMeta(**m.__dict__)


@router.delete("/{sid}", status_code=204)
def remove_session(sid: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("sessions:write")),
                   pm=Depends(get_pm),
                   sm=Depends(get_sm)) -> None:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    if not sm.remove(pid, sid):
        raise NotFound(f"session {sid} not found")


@router.get("/{sid}/messages")
def get_messages(sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(require_scope("sessions:read")),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> list[dict]:
    """Return the persisted transcript for hydration after page reload.

    Returns the raw Anthropic-format message list. The frontend converts
    these into its ChatMessage shape (merging text + tool_use blocks into
    a single assistant bubble and pairing tool_use_ids with the tool_result
    that follows in the next user turn).
    """
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)
    if sid not in {m.session_id for m in project.storage.list_sessions(pid)}:
        raise NotFound(f"session {sid} not found")
    msgs = project.storage.load_messages(pid, sid)
    # Drop non-serializable bits (shouldn't happen with JSON-on-disk, but
    # be defensive — pydantic models, datetimes, etc. would blow up json).
    return [_to_jsonable(m) for m in msgs]


def _to_jsonable(obj):
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_jsonable(v) for v in obj]
    return obj


@router.get("/{sid}/todos")
def get_todos(sid: str = Path(...),
              pid: str = Path(...),
              tid: str = Depends(require_scope("sessions:read")),
              pm=Depends(get_pm),
              sm=Depends(get_sm)) -> list[dict]:
    """Return persisted todos for the session — hydrates the task board
    on page reload. Same shape the loop emits via ``todos_updated`` events."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)
    if sid not in {m.session_id for m in project.storage.list_sessions(pid)}:
        raise NotFound(f"session {sid} not found")
    return project.storage.load_todos(pid, sid)


@router.post("/{sid}/send")
def send_message(body: SendMessageRequest,
                 sid: str = Path(...),
                 pid: str = Path(...),
                 tid: str = Depends(check_rate_limit_scope("sessions:write")),
                 last_event_id: str | None = Header(default=None, alias="Last-Event-Id"),
                 pm=Depends(get_pm),
                 sm=Depends(get_sm)) -> StreamingResponse:
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)

    # Auto-resume: cold sessions are warmed here. 404 only if neither
    # in-memory nor on-disk.
    try:
        sess = sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")

    # 409 if another send is currently holding the project lock.
    if not sm.try_lock(pid):
        raise Conflict(f"project {pid} is busy",
                       details={"code": "project_busy"})

    # B8 reconnect: if the client supplied Last-Event-Id, replay events
    # they missed before streaming fresh ones. The event log is
    # per-session, append-only, persisted to disk on every emit.
    replay: list[dict] = []
    last_seq = 0
    if last_event_id:
        try:
            last_seq = int(last_event_id)
        except (TypeError, ValueError):
            last_seq = 0
    try:
        replay = sess.loop.project.storage.read_session_events_since(
            pid, sid, last_seq)
    except Exception:
        replay = []

    def sync_iter():
        try:
            for ev in sm.send(pid, sid, body.user_input):
                # Persist every emitted event so a reconnecting client
                # can replay from disk. Best-effort: a write failure
                # doesn't break the live stream.
                try:
                    sess.loop.project.storage.append_session_event(
                        pid, sid, ev)
                except Exception:
                    pass
                yield ev
        except Exception as e:
            err = {"type": "error",
                   "message": f"{type(e).__name__}: {e}"}
            try:
                sess.loop.project.storage.append_session_event(pid, sid, err)
            except Exception:
                pass
            yield err

    def _on_cancel():
        # Client disconnect: tell the loop to stop at the next iteration.
        try:
            sess.stop()
        except Exception:
            pass

    return StreamingResponse(
        sse_stream(sync_iter(),
                   on_cancel=_on_cancel,
                   last_event_id=last_seq or None,
                   replay=replay),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )


# ── F7.1 Share links (read-only, signed-token) ───────────────────────

share_router = APIRouter(tags=["share"])


@share_router.post(
    "/tenants/{tid}/projects/{pid}/sessions/{sid}/share")
def create_share_link(sid: str = Path(...),
                      pid: str = Path(...),
                      tid: str = Depends(require_scope("sessions:read")),
                      pm=Depends(get_pm),
                      sm=Depends(get_sm)) -> dict:
    """Mint a signed share token granting read-only access to this
    session's transcript + future assistant turns. The token is
    self-contained — no DB lookup required at verify time."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)
    if sid not in {m.session_id for m in project.storage.list_sessions(pid)}:
        raise NotFound(f"session {sid} not found")
    from ...sharing.tokens import issue_share_token, warn_if_default_secret
    token = issue_share_token(pid, sid, mode="read")
    return {
        "token": token,
        "expires_in": 7 * 24 * 3600,
        "default_secret_in_use": warn_if_default_secret(),
    }


@share_router.get("/shared/{token}/embed")
def shared_embed(token: str) -> Response:
    """F7.3: minimal read-only embed page. Renders the transcript as
    static HTML so it can be iframed into docs / external sites. The
    page itself contains no secrets — it fetches /shared/{token}/messages
    client-side using the token in the URL.

    Returns ``Content-Disposition: inline`` so the iframe renders
    rather than downloading. CSP is intentionally restrictive: no
    external origins, no inline event handlers."""
    from ...sharing.tokens import BadShareToken, verify_share_token
    try:
        verify_share_token(token)
    except BadShareToken as e:
        from ..errors import Unauthorized
        raise Unauthorized(f"invalid share token: {e}")
    body = _EMBED_HTML.replace("__TOKEN__", token)
    return Response(
        content=body,
        media_type="text/html",
        headers={
            # frame-ancestors '*' is the modern CSP equivalent of the
            # deprecated X-Frame-Options: ALLOWALL. Embedding in third-
            # party pages is the product intent (the token in the URL is
            # the authorization, not a cookie). Publishing the directive
            # explicitly is the audit fix — silent fallback was a smell.
            # The watermark below is the user-facing signal that the
            # rendered content is a frozen share.
            "Content-Security-Policy": "default-src 'self'; "
                                       "connect-src 'self'; "
                                       "style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data:; "
                                       "frame-ancestors *;",
            "Cache-Control": "no-store",
        },
    )


@share_router.get("/shared/{token}/messages")
def shared_messages(token: str, request: Request) -> list[dict]:
    """Public read-only endpoint: return the transcript referenced by a
    signed share token. No API key required — the token IS the
    authorization."""
    from ...sharing.tokens import BadShareToken, verify_share_token
    try:
        claims = verify_share_token(token)
    except BadShareToken as e:
        from ..errors import Unauthorized
        raise Unauthorized(f"invalid share token: {e}")
    pm = request.app.state.pm
    try:
        project = pm.get(claims.project_id)
    except KeyError as e:
        from ..errors import NotFound
        raise NotFound(str(e) or "project not found")
    if claims.session_id not in {m.session_id
                                  for m in project.storage.list_sessions(claims.project_id)}:
        from ..errors import NotFound
        raise NotFound("session not found")
    msgs = project.storage.load_messages(claims.project_id, claims.session_id)
    return [_to_jsonable(m) for m in msgs]


_EMBED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mini_cc shared session</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 1rem; line-height: 1.5; }
  .msg { margin: 0.5rem 0; padding: 0.75rem; border-radius: 0.4rem;
         white-space: pre-wrap; word-break: break-word; }
  .user { background: rgba(127,127,127,0.12); }
  .assistant { background: rgba(80,140,220,0.12); }
  .role { font-weight: 600; font-size: 0.8rem; opacity: 0.7;
          text-transform: uppercase; margin-bottom: 0.25rem; }
  #err { color: #c00; }
  .watermark { display: block; font-size: 0.75rem; opacity: 0.65;
               padding: 0.4rem 0.6rem; margin-bottom: 0.5rem;
               border-radius: 0.3rem; background: rgba(127,127,127,0.12);
               text-transform: uppercase; letter-spacing: 0.04em; }
</style>
</head>
<body>
<div class="watermark">Shared (read-only) — frozen session view</div>
<div id="err"></div>
<div id="log"></div>
<script>
const TOKEN = "__TOKEN__";
function fail(msg) {
  document.getElementById("err").textContent = String(msg);
}
async function main() {
  try {
    const r = await fetch("/shared/" + TOKEN + "/messages",
                          { headers: { "Accept": "application/json" } });
    if (!r.ok) { fail("Failed to load: " + r.status); return; }
    const msgs = await r.json();
    const log = document.getElementById("log");
    if (!Array.isArray(msgs) || msgs.length === 0) {
      log.textContent = "(session is empty)";
      return;
    }
    for (const m of msgs) {
      const wrap = document.createElement("div");
      wrap.className = "msg " + (m.role === "user" ? "user" : "assistant");
      const role = document.createElement("div");
      role.className = "role";
      role.textContent = m.role || "?";
      const body = document.createElement("div");
      body.textContent = flatten(m.content);
      wrap.appendChild(role);
      wrap.appendChild(body);
      log.appendChild(wrap);
    }
  } catch (e) { fail(e); }
}
function flatten(c) {
  if (c == null) return "";
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c.map(b => {
      if (!b || typeof b !== "object") return "";
      if (b.type === "text") return b.text || "";
      if (b.type === "tool_use")
        return "[" + (b.name || "tool") + "]";
      if (b.type === "tool_result") return "(tool result)";
      return "";
    }).join("\n");
  }
  return JSON.stringify(c);
}
main();
</script>
</body>
</html>
"""
