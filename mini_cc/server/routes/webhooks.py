"""F7.2 — Webhook registration routes (project-scoped).

Webhooks are persisted per project at
``<state_root>/<project_id>/webhooks.json`` via WebhookRegistry. They
fire on AgentLoop events via WebhookDispatcher, which wraps the
session's on_event callback at session-start time.

Wire-up: SessionManager should install the dispatcher when warming a
session. The handlers here only manage the registry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from ...sharing.webhooks import WebhookRegistry
from ..deps import get_pm, require_scope, validate_id
from ..errors import NotFound

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/webhooks",
    tags=["webhooks"],
)


def _registry_for(pm, pid: str) -> WebhookRegistry:
    project = pm.get(pid)
    # storage.root is the FSStorage state_root. We use the same dir
    # layout (per-project subdir) so backups sweep webhooks too.
    storage_root = getattr(project.storage, "root", None)
    if storage_root is None:
        raise NotFound("webhook storage unavailable")
    return WebhookRegistry(storage_root, pid)


class CreateWebhookRequest(BaseModel):
    url: str = Field(..., description="https:// URL to receive POST events.")
    event_types: list[str] = Field(
        default_factory=list,
        description="Filter; empty list subscribes to all event types.")


class WebhookOut(BaseModel):
    id: str
    url: str
    event_types: list[str]
    created_at: str


def _to_out(h) -> WebhookOut:
    return WebhookOut(id=h.id, url=h.url,
                      event_types=list(h.event_types),
                      created_at=h.created_at)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(pid: str = Path(...),
                  tid: str = Depends(require_scope("projects:read")),
                  pm=Depends(get_pm)) -> list[WebhookOut]:
    validate_id(pid)
    reg = _registry_for(pm, pid)
    return [_to_out(h) for h in reg.list()]


@router.post("", response_model=WebhookOut, status_code=201)
def create_webhook(body: CreateWebhookRequest,
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:write")),
                   pm=Depends(get_pm)) -> WebhookOut:
    validate_id(pid)
    reg = _registry_for(pm, pid)
    try:
        hook = reg.add(body.url, body.event_types)
    except ValueError as e:
        from ..errors import BadRequest
        raise BadRequest(str(e))
    return _to_out(hook)


@router.delete("/{hook_id}", status_code=204)
def delete_webhook(hook_id: str = Path(...),
                   pid: str = Path(...),
                   tid: str = Depends(require_scope("projects:write")),
                   pm=Depends(get_pm)) -> None:
    validate_id(pid)
    reg = _registry_for(pm, pid)
    if not reg.remove(hook_id):
        raise NotFound(f"webhook {hook_id} not found")
