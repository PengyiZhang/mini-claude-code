"""AnthropicProvider.stream hydrates {type:image, asset_id} blocks into
Anthropic base64 image blocks before calling client.messages.stream."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_cc.assets import AssetStore
from mini_cc.core.llm import AnthropicProvider, _hydrate_messages


def _png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
        "4e44ae426082"
    )


def test_hydrate_expands_image_block(tmp_path: Path):
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="test")
    msgs = [
        {"role": "user",
         "content": [
             {"type": "text", "text": "what is this?"},
             {"type": "image", "asset_id": aid},
         ]},
    ]
    out = _hydrate_messages(msgs, store)
    block = out[0]["content"][1]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert "data" in block["source"]


def test_hydrate_passthrough_string_content():
    store = MagicMock()
    msgs = [{"role": "user", "content": "plain string"}]
    out = _hydrate_messages(msgs, store)
    assert out == msgs
    store.hydrate_block.assert_not_called()


def test_hydrate_skips_unknown_asset_id(tmp_path: Path):
    store = AssetStore(tmp_path)
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": "nonexistent"}]}]
    out = _hydrate_messages(msgs, store)
    # Unknown → replaced with a text placeholder (not dropped, so message
    # count stays aligned).
    assert out[0]["content"][0] == {"type": "text", "text": "[image: missing]"}


def test_stream_passes_hydrated_messages(tmp_path: Path):
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    captured = {}

    class _FinalMessage:
        """Mimics anthropic SDK's final message object — needs content +
        stop_reason + usage for AnthropicProvider.stream to build the
        message_stop StreamEvent."""
        content = []
        stop_reason = "end_turn"

        class _Usage:
            input_tokens = 0
            output_tokens = 0
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
        usage = _Usage()

    class FakeStream:
        """anthropic SDK's MessageStreamctx manager: enters as itself,
        iterates as an event stream, then exposes get_final_message()."""
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __iter__(self): return iter([])
        def get_final_message(self): return _FinalMessage()

    class FakeClient:
        @property
        def messages(self):
            class M:
                @staticmethod
                def stream(*, model, system, messages, tools, max_tokens):
                    captured["messages"] = messages
                    return FakeStream()
            return M()

    provider = AnthropicProvider(client_factory=lambda: FakeClient())
    list(provider.stream(model="m", system="s",
                         messages=[{"role": "user",
                                    "content": [{"type": "image",
                                                 "asset_id": aid}]}],
                         tools=[], max_tokens=10,
                         asset_store=store))
    assert captured["messages"][0]["content"][0]["source"]["type"] == "base64"
