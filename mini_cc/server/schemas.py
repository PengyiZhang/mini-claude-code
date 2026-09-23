"""Pydantic request/response models for the HTTP transport."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    project_id: str | None = Field(
        default=None,
        description="Optional caller-supplied project_id. Must match "
                    "[A-Za-z0-9_-]+. If omitted, a random one is generated.")
    display_name: str | None = None
    template: str | None = Field(
        default=None,
        description="Optional template name (see /templates). When set, "
                    "the template's seed files are copied into the new "
                    "project's workspace at creation time.")


class CreateSessionRequest(BaseModel):
    session_id: str | None = None
    model: str | None = None


class SendMessageRequest(BaseModel):
    user_input: str = Field(..., description="The user's turn text.")
    # B8 resume mode: when true, the handler skips the LLM dispatch and
    # only replays events from the per-session log starting after
    # ``Last-Event-Id``. Used by clients that dropped mid-stream and
    # want to recover missed events without triggering a duplicate run.
    resume: bool = Field(default=False, description="Replay-only mode.")


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


class PermissionRequestOut(BaseModel):
    request_id: str
    session_id: str
    tool_name: str
    tool_input: dict
    created_at: str


class DecidePermissionRequest(BaseModel):
    decision: str = Field(..., description='"allow" or "deny"')
    message: str | None = Field(
        default=None,
        description="Optional deny reason shown to the model as the "
                    "tool_result content.")


# ── Admin / keys management (Phase F) ────────────────────────────────

class KeyOut(BaseModel):
    key: str
    key_hint: str = ""
    tenant_id: str
    scopes: list[str]
    created_at: str
    expires_at: str | None
    label: str
    rotated_from: str | None


class CreateKeyRequest(BaseModel):
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str = ""


class UpdateKeyRequest(BaseModel):
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str | None = None


class RotateKeyRequest(BaseModel):
    grace_hours: int = 0
    scopes: list[str] | None = None
    expires_in: str | None = None
    label: str | None = None


class RotateKeyOut(BaseModel):
    new_key: KeyOut
    old_key: KeyOut | None = None  # None when hard-revoked (grace_hours=0)
