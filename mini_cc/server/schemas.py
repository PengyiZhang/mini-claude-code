"""Pydantic request/response models for the HTTP transport."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    project_id: str | None = Field(
        default=None,
        description="Optional caller-supplied project_id. Must match "
                    "[A-Za-z0-9_-]+. If omitted, a random one is generated.")
    display_name: str | None = None


class CreateSessionRequest(BaseModel):
    session_id: str | None = None
    model: str | None = None


class SendMessageRequest(BaseModel):
    user_input: str = Field(..., description="The user's turn text.")


class ProjectOut(BaseModel):
    project_id: str
    tenant_id: str
    display_name: str
    created_at: str


class SessionOut(BaseModel):
    project_id: str
    session_id: str
    created: bool = Field(
        default=True,
        description="False when this call re-warmed an existing session "
                    "(idempotent resume); True when a new session was created.")


class SessionMeta(BaseModel):
    session_id: str
    created_at: str
    last_active_at: str
    message_count: int
    in_memory: bool
