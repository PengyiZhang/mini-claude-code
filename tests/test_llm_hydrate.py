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


# ── Vision gating ───────────────────────────────────────────────────────
# When the configured model can't process images natively
# (MINI_CC_MODEL_VISION=false), the hydrator swaps each image block for
# a text block pointing at a freshly-minted signed URL. The model is
# then expected to pass that URL to whatever vision MCP tool the
# deployment has configured.

def test_hydrate_swaps_image_for_text_url_when_vision_disabled(tmp_path: Path):
    """Defining behavior: vision_capable=False + minter → image block
    becomes a text block carrying the signed URL."""
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": aid}]}]
    minted = []

    def minter(asset_id):
        url = f"http://test.invalid/shared/asset/tok-{asset_id}"
        minted.append((asset_id, url))
        return url

    out = _hydrate_messages(msgs, store,
                            vision_capable=False,
                            asset_url_minter=minter)
    block = out[0]["content"][0]
    assert block["type"] == "text"
    assert "http://test.invalid/shared/asset/tok-" in block["text"]
    assert aid in block["text"]
    assert minted == [(aid, f"http://test.invalid/shared/asset/tok-{aid}")]


def test_hydrate_image_url_text_includes_use_vision_tool_hint(tmp_path: Path):
    """The text block must signal to the model that it should call a
    vision tool — otherwise the model may just respond with the URL."""
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": aid}]}]
    out = _hydrate_messages(msgs, store,
                            vision_capable=False,
                            asset_url_minter=lambda a: f"http://x/{a}")
    text = out[0]["content"][0]["text"].lower()
    assert "vision" in text or "image tool" in text


def test_hydrate_vision_capable_true_unchanged(tmp_path: Path):
    """Default behavior (vision_capable=True) must remain: base64 image
    block passed through to the model. Backwards compat for any
    deployment that doesn't set MINI_CC_MODEL_VISION."""
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": aid}]}]
    out = _hydrate_messages(msgs, store,
                            vision_capable=True,
                            asset_url_minter=lambda a: f"http://x/{a}")
    block = out[0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"


def test_hydrate_vision_disabled_without_minter_falls_back_to_image(tmp_path: Path):
    """If vision_capable=False but no minter is configured, fall back
    to embedding the image — better than dropping it entirely. This
    covers the case where public_base_url isn't set."""
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": aid}]}]
    out = _hydrate_messages(msgs, store,
                            vision_capable=False,
                            asset_url_minter=None)
    block = out[0]["content"][0]
    assert block["type"] == "image"  # fallback path


def test_hydrate_minter_returns_none_falls_back_to_image(tmp_path: Path):
    """If the minter declines (e.g. no public_base_url configured),
    fall back to embedding the image so the request still goes through."""
    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": aid}]}]
    out = _hydrate_messages(msgs, store,
                            vision_capable=False,
                            asset_url_minter=lambda a: None)
    block = out[0]["content"][0]
    assert block["type"] == "image"
