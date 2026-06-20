"""FastAPI dependencies: auth, ID validation, registry access."""
from __future__ import annotations

import re

from fastapi import Header, Path, Request

from ..auth import TenantKeyRegistry
from .errors import BadRequest, Forbidden, Unauthorized

# Same rule as projects.manager._SAFE_ID but local so the server layer
# validates BEFORE the SDK ever sees the value (defense in depth).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def get_registry(request: Request) -> TenantKeyRegistry:
    """Pull the TenantKeyRegistry installed on the app state."""
    reg = getattr(request.app.state, "key_registry", None)
    if reg is None:
        raise RuntimeError("key_registry not configured on app.state")
    return reg


def _parse_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def require_tenant(
    request: Request,
    tid: str = Path(...),
    authorization: str | None = Header(default=None),
) -> str:
    """Resolve the bearer key → tenant_id; enforce it matches {tid}.

    Raises 401 on missing/unknown key, 403 if the resolved tenant does
    not match the {tid} path parameter.
    """
    key = _parse_bearer(authorization)
    if not key:
        raise Unauthorized("missing bearer token")
    reg = get_registry(request)
    resolved = reg.lookup(key)
    if resolved is None:
        raise Unauthorized("unknown api key")
    if resolved != tid:
        raise Forbidden("api key does not match tenant")
    return tid


def validate_id(value: str) -> str:
    """Reject any path ID that isn't [A-Za-z0-9_-]+. Defends against
    path-traversal via crafted project_id / session_id values."""
    if not value or not _SAFE_ID.match(value):
        raise BadRequest(f"invalid id: {value!r}")
    return value


def get_pm(request: Request):
    return request.app.state.pm


def get_sm(request: Request):
    return request.app.state.sm
