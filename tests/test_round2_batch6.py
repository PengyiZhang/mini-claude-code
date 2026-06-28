"""Round 2 audit Batch 6 tests — hook fault tolerance (B3), log
redaction (B9), distributed rate limit fallback (B7)."""
from __future__ import annotations

import io
import logging
import time

import pytest

from mini_cc.core.hooks import Hooks


# ── B3: hooks are fault-tolerant ───────────────────────────────────────────

def test_hook_exception_does_not_break_trigger():
    """A buggy hook that raises must not prevent later hooks from
    running or block the tool call. The exception is logged + skipped."""
    h = Hooks()
    fired = []

    def boom(name, inp):
        raise RuntimeError("hook crashed")

    def innocent(name, inp):
        fired.append(name)
        return None

    h.register(Hooks.PreToolUse, boom)
    h.register(Hooks.PreToolUse, innocent)
    result = h.trigger(Hooks.PreToolUse, "bash", {"command": "ls"})
    # innocent still ran, even though boom exploded.
    assert fired == ["bash"]
    # No denial surfaced.
    assert result is None


def test_hook_returning_denial_still_works_after_exception():
    """If the second hook denies, the denial must propagate even when
    the first hook threw."""
    h = Hooks()

    def boom(name, inp):
        raise ValueError("boom")

    def deny(name, inp):
        return "blocked by policy"

    h.register(Hooks.PreToolUse, boom)
    h.register(Hooks.PreToolUse, deny)
    result = h.trigger(Hooks.PreToolUse, "bash", {"command": "rm -rf /"})
    assert result == "blocked by policy"


def test_hook_exception_logged(caplog):
    """The exception must be logged so operators can see what broke."""
    h = Hooks()

    def boom(name, inp):
        raise RuntimeError("audit-trace explosion")

    h.register(Hooks.PostToolUse, boom)
    with caplog.at_level(logging.ERROR, logger="mini_cc.core.hooks"):
        h.trigger(Hooks.PostToolUse, "bash", {"command": "ls"}, "out")
    assert any("hook.exception" in r.message for r in caplog.records)


# ── B9: log redaction ──────────────────────────────────────────────────────

def test_log_redaction_filters_api_keys():
    """Sensitive patterns must not appear in log output."""
    from mini_cc.server.logging_config import RedactingFilter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("test.redact.1")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        log.info("calling provider with api-key=mck_abcdef1234567890")
        log.info("auth bearer Bearer eyJhbGc.iOi.JKV1")
        log.info("legacy key sk-ant-api03-XXXXX")
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert "mck_abcdef1234567890" not in out
    assert "Bearer eyJhbGc" not in out
    assert "sk-ant-api03-XXXXX" not in out
    # Each pattern should leave a placeholder (format varies per pattern).
    assert out.count("[REDACTED") >= 3


def test_log_redaction_preserves_non_sensitive_text():
    from mini_cc.server.logging_config import RedactingFilter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("test.redact.2")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        log.info("regular message with no secrets")
    finally:
        log.removeHandler(handler)
    assert "regular message with no secrets" in stream.getvalue()


# ── B7: distributed rate limit (memory fallback) ───────────────────────────

def test_tenant_rate_limiter_in_memory_still_works():
    """The default in-memory limiter must keep working when no Redis
    URL is configured. This guards the fallback path."""
    from mini_cc.server.ratelimit import TenantRateLimiter
    rl = TenantRateLimiter(default_rpm=3)
    tid = "tenant-a"
    assert rl.allow(tid)[0] is True   # 1/3
    assert rl.allow(tid)[0] is True   # 2/3
    assert rl.allow(tid)[0] is True   # 3/3
    assert rl.allow(tid)[0] is False  # over


def test_tenant_rate_limiter_redis_factory_returns_memory_when_unreachable():
    """If Redis is requested but unavailable, the factory must fall
    back to the in-memory implementation rather than crash."""
    from mini_cc.server.ratelimit import make_rate_limiter
    rl = make_rate_limiter(
        default_rpm=2,
        redis_url="redis://nonexistent.invalid:6379/0",
        fallback_on_error=True)
    # The fallback must work as a rate limiter.
    assert rl.allow("t1")[0] is True
    assert rl.allow("t1")[0] is True
    assert rl.allow("t1")[0] is False
