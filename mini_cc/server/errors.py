"""Server-side error envelope and exception classes.

Endpoints raise these; a single FastAPI exception handler renders them
as:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""
from __future__ import annotations

from typing import Any


class MiniCCError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(MiniCCError):
    status_code = 404
    code = "not_found"


class Conflict(MiniCCError):
    status_code = 409
    code = "conflict"


class BadRequest(MiniCCError):
    status_code = 400
    code = "bad_request"


class Unauthorized(MiniCCError):
    status_code = 401
    code = "unauthorized"


class Forbidden(MiniCCError):
    status_code = 403
    code = "forbidden"


def envelope(exc: MiniCCError) -> dict[str, Any]:
    return {"error": {
        "code": exc.code,
        "message": exc.message,
        "details": exc.details,
    }}


def map_sdk_exception(e: Exception) -> MiniCCError:
    """Translate SDK exceptions (KeyError / ValueError) into MiniCCError
    subclasses with the right status code."""
    if isinstance(e, MiniCCError):
        return e
    if isinstance(e, KeyError):
        return NotFound(str(e) or "not found")
    msg = str(e)
    if isinstance(e, ValueError):
        if "already exists" in msg:
            return Conflict(msg)
        return BadRequest(msg)
    return MiniCCError(msg or "internal error")
