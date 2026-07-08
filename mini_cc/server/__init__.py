"""Public surface for the HTTP/SSE server.

`build_app` is exposed lazily so importing this package doesn't drag
the full FastAPI route graph in. Routes import lower-level modules
(e.g. ``mini_cc.assets``) that themselves need ``BadRequest`` from
``.errors``; eagerly importing ``app`` here would force
``routes.assets`` → ``mini_cc.assets`` → ``.errors`` → ``app`` and
deadlock mid-initialization. Deferring until ``build_app`` is actually
requested breaks the cycle.
"""
from .errors import (BadRequest, Conflict, Forbidden, MiniCCError, NotFound,
                     Unauthorized)
from .schemas import (CreateProjectRequest, CreateSessionRequest, ProjectOut,
                      SendMessageRequest, SessionOut)
from .sse import sse_stream

__all__ = [
    "build_app",
    "MiniCCError", "NotFound", "Conflict", "BadRequest",
    "Unauthorized", "Forbidden",
    "CreateProjectRequest", "CreateSessionRequest",
    "SendMessageRequest", "ProjectOut", "SessionOut",
    "sse_stream",
]


def __getattr__(name):
    if name == "build_app":
        from .app import build_app as _ba
        return _ba
    raise AttributeError(name)
