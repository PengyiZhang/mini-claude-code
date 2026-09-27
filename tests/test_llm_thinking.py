"""AnthropicProvider.stream forwards thinking_delta events, not just
text_delta. Without this, the Web UI never sees extended-thinking
content."""
from __future__ import annotations

from mini_cc.core.llm import AnthropicProvider, StreamEvent


class _Delta:
    """Mimics anthropic SDK delta shapes (text_delta / thinking_delta)."""
    def __init__(self, dtype: str, text: str = "", thinking: str = ""):
        self.type = dtype
        self.text = text
        self.thinking = thinking


class _Event:
    """Mimics anthropic SDK stream events consumed by AnthropicProvider.stream."""
    def __init__(self, etype: str, delta: _Delta | None = None):
        self.type = etype
        self.delta = delta


class _Usage:
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FinalMessage:
    """Final message returned by stream.get_final_message()."""
    def __init__(self, blocks: list[dict]):
        self.content = blocks
        self.stop_reason = "end_turn"
        self.usage = _Usage()


class FakeStream:
    """anthropic SDK MessageStream ctx manager."""
    def __init__(self, events: list, final_blocks: list[dict]):
        self._events = events
        self._final_blocks = final_blocks

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self._events)
    def get_final_message(self): return _FinalMessage(self._final_blocks)


class FakeClient:
    def __init__(self, events: list, final_blocks: list[dict]):
        self._events = events
        self._final_blocks = final_blocks

    @property
    def messages(self):
        events = self._events
        final_blocks = self._final_blocks

        class M:
            @staticmethod
            def stream(*, model, system, messages, tools, max_tokens):
                return FakeStream(events, final_blocks)
        return M()


def test_stream_forwards_thinking_delta_then_text_delta():
    """Pin Bug 1 fix: a thinking_delta followed by a text_delta must both
    flow through AnthropicProvider.stream, in order, as StreamEvents."""
    events = [
        _Event("content_block_delta", _Delta("thinking_delta", thinking="reasoning…")),
        _Event("content_block_delta", _Delta("text_delta", text="answer")),
    ]
    final_blocks = [
        {"type": "thinking", "thinking": "reasoning…", "signature": "sig"},
        {"type": "text", "text": "answer"},
    ]
    client = FakeClient(events, final_blocks)
    provider = AnthropicProvider(client_factory=lambda: client)

    out = list(provider.stream(model="m", system="s", messages=[],
                                tools=[], max_tokens=10))

    # Final event is always message_stop; everything before is delta flow.
    delta_kinds = [e.kind for e in out if e.kind != "message_stop"]
    assert delta_kinds == ["thinking_delta", "text_delta"]
    thinking_ev = out[0]
    text_ev = out[1]
    assert thinking_ev.kind == "thinking_delta"
    assert thinking_ev.thinking == "reasoning…"
    assert thinking_ev.text is None
    assert text_ev.kind == "text_delta"
    assert text_ev.text == "answer"
    # message_stop still carries the persisted blocks (signature included).
    stop = [e for e in out if e.kind == "message_stop"][0]
    assert stop.content_blocks == final_blocks


def test_stream_skips_empty_thinking_delta():
    """An empty thinking_delta should not produce a StreamEvent — guards
    against empty UI state churn."""
    events = [
        _Event("content_block_delta", _Delta("thinking_delta", thinking="")),
        _Event("content_block_delta", _Delta("text_delta", text="answer")),
    ]
    client = FakeClient(events, [{"type": "text", "text": "answer"}])
    provider = AnthropicProvider(client_factory=lambda: client)
    out = list(provider.stream(model="m", system="s", messages=[],
                                tools=[], max_tokens=10))
    delta_kinds = [e.kind for e in out if e.kind != "message_stop"]
    assert delta_kinds == ["text_delta"]


def test_stream_event_kind_literal_includes_thinking_delta():
    """Pin the StreamEvent dataclass shape so a refactor can't silently
    drop thinking_delta from the Literal."""
    se = StreamEvent(kind="thinking_delta", thinking="x")
    assert se.kind == "thinking_delta"
    assert se.thinking == "x"
