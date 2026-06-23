"""Phase E — Anthropic token usage is recorded into MetricsRegistry.

Drives the real AgentLoop with a mocked Anthropic client that returns
a script of responses with synthetic `usage` blocks. After the turn,
we assert that each kind of token (input/output/cache_read/cache_create)
landed in the corresponding counter, and that the status counter
increments to "success".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mini_cc.config import set_default_config
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.server.metrics import default_registry
from mini_cc.storage import FSStorage


class _UsageResponse:
    """Mock Anthropic response with a usage block + a text content."""
    def __init__(self, *, input_tokens, output_tokens,
                 cache_read=0, cache_create=0,
                 stop_reason="end_turn"):
        self.usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
        )
        self.stop_reason = stop_reason
        self.content = [SimpleNamespace(type="text", text="ok")]


class _MockClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

        class _Stream:
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

        outer = self

        class _M:
            def stream(self_inner, **kw):
                outer.calls.append(kw)
                if not outer._responses:
                    raise RuntimeError("script exhausted")
                return _Stream(outer._responses.pop(0))

        self.messages = _M()


class _MockConfig:
    def __init__(self, client):
        self._client = client

    def build_client(self):
        return self._client

    def build_provider(self):
        # Loop calls default_config().build_provider(), which must return a
        # provider-shaped object (.stream(...)). The mock client itself is
        # anthropic-SDK-shaped (.messages.stream() context manager), so we
        # wrap it in the real AnthropicProvider — its stream() already
        # knows how to drive the SDK-style mock via messages.stream() +
        # get_final_message().
        from mini_cc.core.llm import AnthropicProvider
        return AnthropicProvider(self.build_client)

    api_key = None
    base_url = None
    primary_model = "claude-test"
    fallback_model = None


class _FakeStorage:
    """Minimal storage stub for AgentLoop."""
    def load_messages(self, *args, **kwargs):
        return []

    def load_todos(self, *args, **kwargs):
        return []

    def save_messages(self, *args, **kwargs):
        pass

    def save_todos(self, *args, **kwargs):
        pass


@pytest.fixture
def reset_config(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir(exist_ok=True)
    yield storage_root
    import mini_cc.config as cfg
    cfg._DEFAULT = None


def _drive_loop(metrics, responses, *, tenant="t1", storage_root=None):
    client = _MockClient(responses)
    set_default_config(_MockConfig(client))  # type: ignore[arg-type]
    ref = ProjectRef(
        project_id="p1",
        project_root=".",
        sandbox=SubprocessSandbox("p1", "."),
        storage=FSStorage(storage_root),
        tenant_id=tenant,
        metrics=metrics,
    )
    loop = AgentLoop(ref, "s1")
    events = list(loop.run("hi"))
    return events


def test_token_usage_recorded_per_kind(reset_config):
    metrics = default_registry()
    events = _drive_loop(
        metrics,
        [_UsageResponse(input_tokens=100, output_tokens=50,
                         cache_read=200, cache_create=30)],
        storage_root=reset_config)
    assert any(ev["type"] == "done" for ev in events)
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="input") == 100
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="output") == 50
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="cache_read") == 200
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="cache_create") == 30


def test_token_usage_accumulates_across_turns(reset_config):
    metrics = default_registry()
    _drive_loop(metrics,
                [_UsageResponse(input_tokens=100, output_tokens=50)],
                storage_root=reset_config)
    _drive_loop(metrics,
                [_UsageResponse(input_tokens=20, output_tokens=10)],
                storage_root=reset_config)
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="input") == 120
    assert metrics.counters["anthropic_tokens_total"].value(
        tenant="t1", kind="output") == 60


def test_request_status_success_recorded(reset_config):
    metrics = default_registry()
    _drive_loop(metrics,
                [_UsageResponse(input_tokens=10, output_tokens=5)],
                storage_root=reset_config)
    assert metrics.counters["anthropic_request_total"].value(
        tenant="t1", status="success") == 1


def test_request_status_error_recorded_on_client_failure(reset_config):
    metrics = default_registry()

    class _ExplodingMessages:
        def stream(self, **kw):
            raise RuntimeError("network down")

    class _ExplodingClient:
        @property
        def messages(self):
            return _ExplodingMessages()

    set_default_config(_MockConfig(_ExplodingClient()))  # type: ignore[arg-type]
    ref = ProjectRef(
        project_id="p1", project_root=".",
        sandbox=SubprocessSandbox("p1", "."),
        storage=FSStorage(reset_config), tenant_id="t1", metrics=metrics,
    )
    loop = AgentLoop(ref, "s1")
    list(loop.run("hi"))
    assert metrics.counters["anthropic_request_total"].value(
        tenant="t1", status="error") == 1


def test_no_metrics_no_crash(reset_config):
    """ProjectRef.metrics=None is the default for tests that predate
    Phase E. The loop must not crash."""
    client = _MockClient([_UsageResponse(input_tokens=10, output_tokens=5)])
    set_default_config(_MockConfig(client))  # type: ignore[arg-type]
    ref = ProjectRef(
        project_id="p1", project_root=".",
        sandbox=SubprocessSandbox("p1", "."),
        storage=FSStorage(reset_config), tenant_id="t1",
        # metrics=None
    )
    loop = AgentLoop(ref, "s1")
    events = list(loop.run("hi"))
    assert any(ev["type"] == "done" for ev in events)
