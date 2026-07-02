"""Slash command routes.

Lists available commands for the frontend autocomplete menu and executes
server-scoped commands, returning an SSE stream of the same shape as
``/sessions/{sid}/send`` so the chat pane can render the output uniformly.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...commands import CommandContext, default_registry
from ...commands.cards import persist_card_event
from ..deps import check_rate_limit_scope, get_pm, get_sm, require_scope, validate_id
from ..errors import NotFound
from ..sse import sse_stream

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/sessions",
    tags=["commands"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


class CommandOut(BaseModel):
    name: str
    description: str
    scope: str
    aliases: list[str]


class RunCommandRequest(BaseModel):
    args: str | None = None
    # When True, the route streams the response live but skips
    # persist_card_event + save_messages. Used by panel-polling helpers
    # (TeammatesPanel's 4s /agents refresh) so the session transcript
    # doesn't accumulate one __card__ bubble per poll tick. Default
    # False preserves the "cards survive refresh" contract for
    # user-initiated slash commands.
    ephemeral: bool = False


@router.get("/{sid}/commands", response_model=list[CommandOut])
def list_commands(pid: str = Path(...),
                  sid: str = Path(...),
                  tid: str = Depends(require_scope("sessions:read")),
                  pm=Depends(get_pm),
                  sm=Depends(get_sm)) -> list[CommandOut]:
    """List commands the frontend should offer in the autocomplete menu."""
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)
    reg = default_registry()
    return [
        CommandOut(name=c.name, description=c.description,
                   scope=c.scope, aliases=list(c.aliases))
        for c in reg.all_visible()
    ]


@router.post("/{sid}/commands/{name}")
def run_command(name: str,
                body: RunCommandRequest = Body(default=RunCommandRequest()),
                sid: str = Path(...),
                pid: str = Path(...),
                tid: str = Depends(check_rate_limit_scope("sessions:write")),
                pm=Depends(get_pm),
                sm=Depends(get_sm)) -> StreamingResponse:
    """Execute a server-scoped slash command.

    Returns an SSE stream with the same event shape as ``POST /send`` so
    the frontend can feed it directly into the chat pane's event handler.
    """
    validate_id(pid)
    validate_id(sid)
    _check_project_tenant(pid, tid, pm)

    # Cold sessions need to be warm so /clear and /compact can mutate
    # loop state. 404 if neither in-memory nor on-disk.
    try:
        sess = sm._ensure_warm(pid, sid)
    except KeyError as e:
        raise NotFound(str(e) or f"session {sid} not found")

    reg = default_registry()
    cmd = reg.resolve(name)
    if cmd is None or cmd.scope != "server":
        raise NotFound(f"command not found: /{name}")
    project = pm.get(pid)

    def sync_iter():
        try:
            ctx = CommandContext(
                project_id=pid,
                session_id=sid,
                tenant_id=tid,
                args=(body.args or ""),
                project=project,
                session_manager=sm,
                storage=project.storage,
            )
            card_buffer: list[dict] = []
            for ev in cmd.handler(ctx):
                # Side-channel persist: each card event is appended to
                # the transcript as a synthetic __card__ tool_use so a
                # page reload rehydrates the same cards. Buffered and
                # flushed once at the end (instead of save_messages per
                # card) so a multi-card command stays O(1) on disk.
                #
                # Skipped when body.ephemeral is set — panel-polling
                # callers (TeammatesPanel /agents every 4s) want the
                # live SSE stream but must not pollute the transcript
                # with one card bubble per tick.
                if isinstance(ev, dict) and ev.get("type") == "card":
                    card_buffer.append(ev)
                yield ev
            if card_buffer and not body.ephemeral:
                for c in card_buffer:
                    persist_card_event(sess.loop.messages, c)
                try:
                    project.storage.save_messages(pid, sid, sess.loop.messages)
                except Exception:
                    # Persistence is best-effort: a transient write
                    # failure shouldn't break the SSE stream the user
                    # is already looking at.
                    pass
        except Exception as e:
            yield {"type": "error",
                   "message": f"{type(e).__name__}: {e}"}

    def _on_cancel():
        try:
            sess.stop()
        except Exception:
            pass

    return StreamingResponse(
        sse_stream(sync_iter(), on_cancel=_on_cancel),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
