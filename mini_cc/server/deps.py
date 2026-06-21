"""FastAPI dependencies: auth, ID validation, registry access, rate limit.

Phase D added per-route scope enforcement via ``require_scope`` /
``check_rate_limit`` factories. Both accept a ``resource:verb`` string
(e.g. ``"sessions:write"``, ``"files:read"``). The verb may be
omitted (``"sessions"``) to mean "any method on this resource"; the
HTTP method of the incoming request is used to compute the effective
verb (GET → read, everything else → write).
"""
from __future__ import annotations

import math
import re

from fastapi import Depends, Header, Path, Request

from ..auth import TenantKeyRegistry
from ..auth.scope import scope_allows
from .errors import BadRequest, Forbidden, TooManyRequests, Unauthorized
from .ratelimit import TenantRateLimiter

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
    """Back-compat shim — equivalent to ``require_scope("*")``.

    Kept so any caller that hasn't migrated yet continues to work.
    """
    return _resolve(request, tid, authorization, required_scope="*")


def require_scope(required: str):
    """Build a dep that resolves the bearer key, enforces tenant match,
    AND verifies the key holds ``required`` scope for the request's HTTP
    method. Returns the resolved tenant_id.

    Raises 401 on missing/unknown/expired key, 403 on tenant mismatch
    or insufficient scope (the latter carries ``WWW-Authenticate`` +
    ``insufficient_scope`` details so clients can introspect).
    """
    def _dep(request: Request,
             tid: str = Path(...),
             authorization: str | None = Header(default=None)) -> str:
        return _resolve(request, tid, authorization, required_scope=required)
    _dep.__name__ = f"require_scope_{required.replace(':', '_').replace('*', 'all')}"
    return _dep


def _resolve(request: Request, tid: str, authorization: str | None,
             required_scope: str) -> str:
    key = _parse_bearer(authorization)
    if not key:
        raise Unauthorized("missing bearer token")
    reg = get_registry(request)
    rec = reg.lookup(key)
    if rec is None:
        raise Unauthorized("unknown or expired api key")
    if rec.tenant_id != tid:
        raise Forbidden("api key does not match tenant")
    if not scope_allows(rec.scopes, required_scope, request.method):
        raise Forbidden(
            f"key lacks required scope: {required_scope}",
            details={"code": "insufficient_scope",
                     "required": required_scope,
                     "held": list(rec.scopes)},
            extra_headers={"WWW-Authenticate":
                           f'Bearer scope="{required_scope}"'})
    # Publish on request.state so the metrics middleware can pick it up
    # without re-resolving the bearer.
    request.state.tenant_id = tid
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


def check_rate_limit(
    request: Request,
    tid: str = Depends(require_tenant),
) -> str:
    """Per-tenant token-bucket gate. Returns tid so routes can chain
    ``Depends(check_rate_limit)`` in place of ``Depends(require_tenant)``.

    On deny raises 429 with ``Retry-After`` in the response headers via
    the error envelope (mapped in errors.py).

    Phase D note: callers wanting per-route scope enforcement should
    prefer ``check_rate_limit_scope(required)`` below; this legacy
    form keeps the ``*`` scope for untouched callers.
    """
    return _apply_rate_limit(request, tid)


def check_rate_limit_scope(required: str):
    """Factory combining ``require_scope(required)`` + rate limit. Use
    in place of ``check_rate_limit`` when the route has a specific
    scope requirement.
    """
    def _dep(request: Request,
             tid: str = Depends(require_scope(required))) -> str:
        return _apply_rate_limit(request, tid)
    _dep.__name__ = f"check_rate_limit_scope_{required.replace(':', '_').replace('*', 'all')}"
    return _dep


def _apply_rate_limit(request: Request, tid: str) -> str:
    limiter: TenantRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None)
    if limiter is None:
        return tid
    allowed, retry_after = limiter.allow(tid)
    if not allowed:
        retry_after_int = max(1, math.ceil(retry_after)) if math.isfinite(retry_after) else 60
        request.state.rate_limit_retry_after = retry_after_int
        raise TooManyRequests(
            "rate limit exceeded",
            details={"code": "rate_limited",
                     "retry_after": retry_after_int})
    return tid
