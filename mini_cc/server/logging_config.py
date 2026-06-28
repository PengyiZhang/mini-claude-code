"""Structured logging: JSON or text, with a request-scoped trace_id.

`trace_id` flows via contextvars so any logger in the same async task
or thread sees it without explicit threading of the value.

configure_logging() is idempotent — safe to call from tests and from
the CLI serve path.

B9: RedactingFilter strips API keys / bearer tokens from log records
before they hit the handler. configure_logging() installs it by
default — credentials should never reach stdout / log aggregators.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

_TRACE_CTX: ContextVar[str | None] = ContextVar("mini_cc_trace_id", default=None)
_TENANT_CTX: ContextVar[str | None] = ContextVar("mini_cc_tenant", default=None)

_CONFIGURED = False


# Sensitive patterns to scrub from log messages. Order matters — more
# specific patterns first so the placeholder replaces the longest match.
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # mck_<token> — mini_cc's own API key format.
    (re.compile(r"mck_[A-Za-z0-9]{12,}"), "[REDACTED:mck_key]"),
    # Bearer <jwt> headers.
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]+"), "Bearer [REDACTED]"),
    # Anthropic-style sk-ant-... keys.
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED:anthropic_key]"),
    # OpenAI-style sk-... keys (don't conflict with sk-ant- which is matched first).
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:openai_key]"),
    # Generic api_key=<value> query/form param.
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*[^\s&]+"), "api_key=[REDACTED]"),
    # Authorization: Basic <base64>.
    (re.compile(r"(?i)basic\s+[A-Za-z0-9+/=]{16,}"), "basic [REDACTED]"),
]


class RedactingFilter(logging.Filter):
    """Strips credential-shaped substrings from log records.

    Filters both the formatted message and the raw ``record.msg`` so
    JSON formatters that re-render getMessage() see the scrubbed
    version. Attach to a handler (preferred) or to a logger.
    """

    def __init__(self, *, placeholder: str = "[REDACTED]"):
        super().__init__()
        self._placeholder = placeholder

    def _scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        out = text
        for rx, repl in _REDACT_PATTERNS:
            out = rx.sub(repl, out)
        return out

    def filter(self, record: logging.LogRecord) -> bool:
        # Scrub the resolved message via getMessage()'s backing fields.
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if isinstance(record.args, dict):
            record.args = {k: self._scrub(v) if isinstance(v, str) else v
                           for k, v in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(
                self._scrub(a) if isinstance(a, str) else a
                for a in record.args)
        # The exception traceback text — if it captured a request
        # payload, scrub there too.
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


def get_trace_id() -> str | None:
    return _TRACE_CTX.get()


def set_trace_id(tid: str | None) -> None:
    _TRACE_CTX.set(tid)


def get_tenant() -> str | None:
    return _TENANT_CTX.get()


def set_tenant(tid: str | None) -> None:
    _TENANT_CTX.set(tid)


def new_trace_id() -> str:
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, trace_id, tenant, extras."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message",
    }

    @staticmethod
    def _iso_ts(record: logging.LogRecord) -> str:
        # Python's time.strftime doesn't support %f (microseconds); build the
        # ISO 8601 string ourselves so we get sub-second precision.
        dt = _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self._iso_ts(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        tid = _TRACE_CTX.get()
        if tid:
            payload["trace_id"] = tid
        tenant = _TENANT_CTX.get()
        if tenant:
            payload["tenant"] = tenant
        # Extra fields attached via logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Plain text with trace_id + tenant prefix when set."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts = []
        tid = _TRACE_CTX.get()
        if tid:
            parts.append(f"trace={tid}")
        tenant = _TENANT_CTX.get()
        if tenant:
            parts.append(f"tenant={tenant}")
        if parts:
            return f"[{' '.join(parts)}] {base}"
        return base


def configure_logging(format: str = "json", level: str = "INFO") -> None:
    """Install a single StreamHandler on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        # Still allow level adjustments on re-config.
        logging.getLogger().setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    if format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Coax uvicorn / fastapi into the same format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True  # let root handler emit

    _CONFIGURED = True
