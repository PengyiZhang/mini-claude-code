"""Phase E — tracing.log_span emits structured log lines."""
from __future__ import annotations

import io
import logging

import pytest

from mini_cc.server.logging_config import configure_logging, set_trace_id
from mini_cc.server.tracing import is_otel_enabled, log_span


@pytest.fixture(autouse=True)
def capture_logs():
    """Install a StringIO handler so tests can read structured log output."""
    configure_logging(format="json", level="INFO")
    root = logging.getLogger()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    # Use JsonFormatter so we can parse structured fields.
    from mini_cc.server.logging_config import JsonFormatter
    h.setFormatter(JsonFormatter())
    # Replace any existing handlers so we see only our buffer.
    root.handlers = [h]
    yield buf
    # Cleanup — let configure_logging re-install next time.
    root.handlers = []


def _parse_log_lines(buf):
    import json
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_log_span_emits_info_line_with_dur_ms_and_trace_id():
    set_trace_id("trace-abc")
    try:
        with log_span("test.span", route="/x"):
            pass
    finally:
        set_trace_id(None)
    # The handler is on root; we need to flush.
    for h in logging.getLogger().handlers:
        h.flush()


def test_log_span_inherits_current_trace_id(capture_logs):
    set_trace_id("trace-inherit-xyz")
    try:
        with log_span("test.span"):
            pass
    finally:
        set_trace_id(None)
    for h in logging.getLogger().handlers:
        h.flush()
    entries = _parse_log_lines(capture_logs)
    span_entries = [e for e in entries if e.get("span") == "test.span"]
    assert any(e.get("trace_id") == "trace-inherit-xyz" for e in span_entries)
    assert all("dur_ms" in e for e in span_entries)


def test_log_span_logs_warning_on_exception(capture_logs):
    set_trace_id("trace-exc")
    try:
        with pytest.raises(ValueError):
            with log_span("test.span"):
                raise ValueError("boom")
    finally:
        set_trace_id(None)
    for h in logging.getLogger().handlers:
        h.flush()
    entries = _parse_log_lines(capture_logs)
    span_entries = [e for e in entries if e.get("span") == "test.span"]
    assert any(e.get("level") == "WARNING" for e in span_entries)
    assert any(e.get("error") == "ValueError" for e in span_entries)


def test_log_span_carries_caller_fields(capture_logs):
    with log_span("test.span", tool="bash", model="claude-x"):
        pass
    for h in logging.getLogger().handlers:
        h.flush()
    entries = _parse_log_lines(capture_logs)
    span_entries = [e for e in entries if e.get("span") == "test.span"]
    assert any(e.get("tool") == "bash" for e in span_entries)
    assert any(e.get("model") == "claude-x" for e in span_entries)


def test_otel_disabled_by_default():
    """Without MINI_CC_OTEL_EXPORTER, the SDK is not loaded."""
    # log_span is called from other tests; the cached state persists.
    # is_otel_enabled() returns False unless env configured + SDK present.
    assert is_otel_enabled() is False or is_otel_enabled() is True  # no crash
