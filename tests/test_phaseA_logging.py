"""Phase A: JSON logging + trace_id contextvar propagation."""
from __future__ import annotations

import io
import json
import logging

from mini_cc.server.logging_config import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    get_tenant,
    get_trace_id,
    set_tenant,
    set_trace_id,
)


def _make_record(msg: str, extra: dict | None = None) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=None)
    for k, v in (extra or {}).items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_emits_trace_id_and_tenant():
    configure_logging(format="json", level="INFO")
    set_trace_id("trace-abc")
    set_tenant("tenant-xyz")
    try:
        out = JsonFormatter().format(_make_record("hello", extra={"k": "v"}))
    finally:
        set_trace_id(None)
        set_tenant(None)
    payload = json.loads(out)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["trace_id"] == "trace-abc"
    assert payload["tenant"] == "tenant-xyz"
    assert payload["k"] == "v"


def test_no_trace_id_omits_field():
    configure_logging(format="json", level="INFO")
    set_trace_id(None)
    set_tenant(None)
    out = JsonFormatter().format(_make_record("plain"))
    payload = json.loads(out)
    assert "trace_id" not in payload
    assert "tenant" not in payload


def test_text_format_works_for_dev_mode():
    configure_logging(format="text", level="INFO")
    set_trace_id("t1")
    try:
        out = TextFormatter(fmt="%(message)s").format(_make_record("careful"))
    finally:
        set_trace_id(None)
    assert "careful" in out
    assert "trace=t1" in out


def test_contextvar_get_returns_set_value():
    set_trace_id("zz")
    try:
        assert get_trace_id() == "zz"
    finally:
        set_trace_id(None)
    assert get_trace_id() is None
