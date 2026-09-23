"""M4-1/M4-2: classify_error 分类、with_retry 快速失败、loop 错误事件带分类。"""
from __future__ import annotations

import pytest

from mini_cc.core.recovery import RecoveryState, classify_error, with_retry


class RateLimitError(Exception): pass
class OverloadedError(Exception): pass
class AuthError(Exception): pass


# ── classify_error ──────────────────────────────────────────────

@pytest.mark.parametrize("exc,kind,transient", [
    (RateLimitError("429 too many requests"), "rate_limit", True),
    (OverloadedError("529 overloaded"), "overloaded", True),
    (ConnectionError("connection reset by peer"), "network", True),
    (TimeoutError("request timed out"), "network", True),
    (RuntimeError("insufficient quota / billing hard limit"), "quota", False),
    (AuthError("invalid_api_key"), "auth", False),
    (AuthError("authentication error 401"), "auth", False),
    (RuntimeError("invalid_request_error: max_tokens > limit"), "invalid_request", False),
    (ValueError("something novel"), "unknown", False),
])
def test_classify_error(exc, kind, transient):
    cls = classify_error(exc)
    assert cls.kind == kind and cls.transient is transient


def test_rate_limit_wins_over_quota_wording():
    # "Rate limit ... quota" 必须按限流处理（与现状 429 优先一致）
    cls = classify_error(RateLimitError("Rate limit reached (TPM quota)"))
    assert cls.kind == "rate_limit" and cls.transient


# ── with_retry 快速失败 ────────────────────────────────────────

def test_with_retry_fail_fast_on_permanent(monkeypatch):
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep",
                        lambda _: pytest.fail("must not sleep on permanent"))
    calls = []
    def fn():
        calls.append(1)
        raise AuthError("invalid_api_key")
    with pytest.raises(AuthError):
        with_retry(fn, RecoveryState())
    assert len(calls) == 1  # 未重试


def test_with_retry_retries_transient_network(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep", sleeps.append)
    state = [0]
    def fn():
        state[0] += 1
        if state[0] < 3:
            raise ConnectionError("connection reset")
        return "ok"
    assert with_retry(fn, RecoveryState()) == "ok"
    assert len(sleeps) == 2


# ── loop 集成 ──────────────────────────────────────────────────

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.core.llm import AnthropicProvider
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage


class _ErrorClient:
    """SDK-shape mock (cf. test_p0_loop_mocked._MockClient) whose
    messages.create/stream raise instead of returning a scripted
    response, so the provider surfaces `exc` at stream-open time."""
    def __init__(self, exc: Exception):
        outer = self

        class _M:
            def create(self_inner, **kw):
                raise exc

            def stream(self_inner, **kw):
                raise exc

        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_error_loop(tmp_path, exc):
    # 照 tests/test_p0_loop_mocked.py 的 _MockClient/_Block/_MockResponse/
    # _build_loop 骨架组装，但 client 的 create/stream 抛 exc 而非返回脚本。
    client = _ErrorClient(exc)
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=lambda: AnthropicProvider(lambda: client))
    return AgentLoop(ref, "sess1")


def test_loop_error_event_carries_classification(tmp_path):
    events = list(_build_error_loop(tmp_path, ConnectionError("connection reset")).run("hi"))
    err = [e for e in events if e["type"] == "error"]
    assert len(err) == 1
    assert err[0]["error_class"] == "network"
    assert err[0]["transient"] is True


def test_loop_transcript_marks_transient(tmp_path):
    loop = _build_error_loop(tmp_path, ConnectionError("connection reset"))
    list(loop.run("hi"))
    texts = [b.get("text", "") for m in loop.messages
             for b in m.get("content", []) if isinstance(b, dict)]
    assert any(t.startswith("[Error][transient]") for t in texts)
