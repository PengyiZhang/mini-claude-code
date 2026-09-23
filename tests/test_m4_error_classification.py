"""M4-1/M4-2: classify_error 分类、with_retry 快速失败、loop 错误事件带分类。"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from mini_cc.core.recovery import RecoveryState, classify_error, with_retry


class RateLimitError(Exception): pass
class OverloadedError(Exception): pass
class AuthError(Exception): pass


class _StatusError(Exception):
    """Exception carrying only an HTTP status_code — message is keyword-
    free, so classification must come from the status attribute."""
    def __init__(self, status_code: int, msg: str = "provider sad"):
        super().__init__(msg)
        self.status_code = status_code


# ── classify_error ──────────────────────────────────────────────

@pytest.mark.parametrize("exc,kind,transient", [
    (RateLimitError("429 too many requests"), "rate_limit", True),
    (OverloadedError("529 overloaded"), "overloaded", True),
    (ConnectionError("connection reset by peer"), "network", True),
    (ConnectionError(), "network", True),
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


@pytest.mark.parametrize("status,kind,transient", [
    (429, "rate_limit", True),
    (529, "overloaded", True),
    (402, "quota", False),
    (401, "auth", False),
    (403, "auth", False),
    (400, "invalid_request", False),
])
def test_classify_error_status_code_is_authoritative(status, kind, transient):
    # 消息不含任何关键词——只有 status_code 可判。status 必须先于
    # 名称/消息启发式被检查。
    cls = classify_error(_StatusError(status))
    assert cls.kind == kind and cls.transient is transient


def test_classify_error_status_429_wins_over_quota_wording():
    # 与 test_rate_limit_wins_over_quota_wording 同一原则：携带 429
    # 状态码的错误即使消息里出现 quota 也必须保持可重试。
    cls = classify_error(_StatusError(429, "monthly quota exhausted"))
    assert cls.kind == "rate_limit" and cls.transient


def test_rate_limit_wins_over_quota_wording():
    # "Rate limit ... quota" 必须按限流处理（与现状 429 优先一致）
    cls = classify_error(RateLimitError("Rate limit reached (TPM quota)"))
    assert cls.kind == "rate_limit" and cls.transient


# ── with_retry 快速失败 ────────────────────────────────────────

def test_with_retry_fail_fast_on_permanent(monkeypatch):
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep",
                        lambda _: pytest.fail("must not sleep on permanent"))
    events = []
    calls = []
    def fn():
        calls.append(1)
        raise AuthError("invalid_api_key")
    with pytest.raises(AuthError):
        with_retry(fn, RecoveryState(), on_event=events.append)
    assert len(calls) == 1  # 未重试
    # 永久错误中止时必须发出带原因类别的 retry_aborted 事件
    assert events == [{"type": "retry_aborted", "reason": "auth",
                       "error": "invalid_api_key"}]


def test_with_retry_429_then_success(monkeypatch):
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep", lambda _: None)
    events = []
    calls = []
    def fn():
        calls.append(1)
        if len(calls) < 2:
            raise RateLimitError("429 too many requests")
        return "ok"
    assert with_retry(fn, RecoveryState(), on_event=events.append) == "ok"
    assert len(calls) == 2
    retries = [e for e in events if e["type"] == "retry"]
    assert len(retries) == 1
    assert retries[0]["reason"] == "429"
    assert retries[0]["attempt"] == 1
    assert retries[0]["delay"] > 0


def test_with_retry_two_529s_switch_to_fallback_model(monkeypatch):
    monkeypatch.setattr("mini_cc.core.recovery.time.sleep", lambda _: None)

    class _Cfg:
        primary_model = "primary-x"
        fallback_model = "fallback-x"

    monkeypatch.setattr("mini_cc.core.recovery.default_config",
                        lambda: _Cfg())
    events = []
    state = RecoveryState()
    assert state.current_model == "primary-x"
    calls = []
    def fn():
        calls.append(state.current_model)
        if len(calls) < 3:
            raise OverloadedError("529 overloaded")
        return "ok"
    assert with_retry(fn, state, on_event=events.append) == "ok"
    # 前两次调用在主模型上失败，第二次 529 后切换到 fallback
    assert calls == ["primary-x", "primary-x", "fallback-x"]
    assert state.current_model == "fallback-x"
    fb = [e for e in events if e["type"] == "fallback_model"]
    assert fb == [{"type": "fallback_model", "model": "fallback-x"}]


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


@dataclass
class _Block:
    type: str
    text: str | None = None


class _MockResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _ErrorClient:
    """SDK-shape mock (cf. test_p0_loop_mocked._MockClient) whose
    messages.create/stream raise ``exc`` instead of returning a
    scripted response, so the provider surfaces `exc` at stream-open
    time. With ``fail_times``/``script`` the first N stream opens fail
    and later ones replay normal responses (raise-then-succeed)."""
    def __init__(self, exc: Exception, fail_times: int | None = None,
                 script: list | None = None):
        # fail_times=None → every call raises（纯错误形态）
        self._exc = exc
        self._fail_times = fail_times
        self._script = list(script or [])
        outer = self

        class _Stream:
            """Context-manager mock matching the SDK's messages.stream()."""
            def __init__(self_inner, response):
                self_inner._response = response
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
            def __iter__(self_inner):
                return iter(())
            def get_final_message(self_inner):
                return self_inner._response
            def close(self_inner):
                pass

        class _M:
            def create(self_inner, **kw):
                raise outer._exc

            def stream(self_inner, **kw):
                if (outer._fail_times is None or outer._fail_times > 0):
                    if outer._fail_times is not None:
                        outer._fail_times -= 1
                    raise outer._exc
                if not outer._script:
                    raise RuntimeError("script exhausted")
                return _Stream(outer._script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


def _build_error_loop(tmp_path, exc, fail_times=None, script=None,
                      on_event=None):
    # 照 tests/test_p0_loop_mocked.py 的 _MockClient/_Block/_MockResponse/
    # _build_loop 骨架组装，但 client 的 create/stream 抛 exc 而非返回脚本。
    client = _ErrorClient(exc, fail_times=fail_times, script=script)
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(project_id="proj-a", project_root=str(tmp_path / "ws"),
                     sandbox=sandbox, storage=storage,
                     client_factory=lambda: AnthropicProvider(lambda: client))
    return AgentLoop(ref, "sess1", on_event=on_event)


def test_loop_error_event_carries_classification(tmp_path, monkeypatch):
    monkeypatch.setattr("mini_cc.core.loop.time.sleep", lambda _: None)
    events = list(_build_error_loop(tmp_path, ConnectionError("connection reset")).run("hi"))
    err = [e for e in events if e["type"] == "error"]
    assert len(err) == 1
    assert err[0]["error_class"] == "network"
    assert err[0]["transient"] is True


def test_loop_transcript_marks_transient(tmp_path, monkeypatch):
    monkeypatch.setattr("mini_cc.core.loop.time.sleep", lambda _: None)
    loop = _build_error_loop(tmp_path, ConnectionError("connection reset"))
    list(loop.run("hi"))
    texts = [b.get("text", "") for m in loop.messages
             for b in m.get("content", []) if isinstance(b, dict)]
    assert any(t.startswith("[Error][transient]") for t in texts)


def test_loop_retries_429_by_status_code_then_completes(tmp_path, monkeypatch):
    # 消息不含 "429"/"ratelimit"——只有 status_code=429 可判；
    # 内联重试循环必须经 classify_error 认出限流并重试至成功。
    monkeypatch.setattr("mini_cc.core.loop.time.sleep", lambda _: None)
    emitted = []
    script = [_MockResponse([_Block(type="text", text="done")])]
    loop = _build_error_loop(tmp_path, _StatusError(429, "slow down"),
                             fail_times=1, script=script,
                             on_event=emitted.append)
    events = list(loop.run("hi"))
    assert events[-1]["type"] == "done"
    assert [e for e in emitted if e["type"] == "retry"] == [
        {"type": "retry", "reason": "429", "attempt": 1}]


def test_loop_retries_network_error_then_completes(tmp_path, monkeypatch):
    # 瞬态网络错误（connection reset）在流打开阶段也必须重试，
    # 而不是直接把回合打爆。
    monkeypatch.setattr("mini_cc.core.loop.time.sleep", lambda _: None)
    emitted = []
    script = [_MockResponse([_Block(type="text", text="done")])]
    loop = _build_error_loop(tmp_path, ConnectionError("connection reset"),
                             fail_times=1, script=script,
                             on_event=emitted.append)
    events = list(loop.run("hi"))
    assert events[-1]["type"] == "done"
    assert [e for e in emitted if e["type"] == "retry"] == [
        {"type": "retry", "reason": "network", "attempt": 1}]


def test_loop_permanent_auth_error_not_retried(tmp_path, monkeypatch):
    # 永久错误（auth）不得重试；事件 transient=False，
    # 转录前缀为朴素的 [Error] （无 [transient] 标记）。
    monkeypatch.setattr("mini_cc.core.loop.time.sleep",
                        lambda _: pytest.fail("must not retry permanent errors"))
    emitted = []
    loop = _build_error_loop(tmp_path, AuthError("invalid_api_key"),
                             on_event=emitted.append)
    events = list(loop.run("hi"))
    err = [e for e in events if e["type"] == "error"]
    assert len(err) == 1
    assert err[0]["error_class"] == "auth"
    assert err[0]["transient"] is False
    assert not [e for e in emitted if e["type"] == "retry"]
    texts = [b.get("text", "") for m in loop.messages
             for b in m.get("content", []) if isinstance(b, dict)]
    assert any(t.startswith("[Error] AuthError: invalid_api_key")
               for t in texts)
    assert not any("[Error][transient]" in t for t in texts)
