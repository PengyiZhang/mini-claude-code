"""LiteLLMProvider.stream must not silently drop user image attachments.

Regression test for the silent-data-loss bug surfaced in the final code
review of the image-input feature: LiteLLMProvider.stream accepted the
``asset_store`` kwarg (added in commit e22dbb6 to fix the cross-provider
signature regression) but never called ``_hydrate_messages`` and its
``_convert_messages`` helper only handled ``text`` + ``tool_result``
user-block types. Net effect: any image sent via a LiteLLM-routed model
(DeepSeek/Qwen/Gemini/etc.) vanished before the API call.

These tests pin two contracts:

1. ``{type:"image", asset_id:"..."}`` blocks hydrate and convert to
   OpenAI ``image_url`` blocks with a data: URI (so the image actually
   reaches the model).
2. Unknown asset_id (hydration returns nothing) degrades to a text
   placeholder rather than dropping the block silently.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mini_cc.assets import AssetStore
from mini_cc.core.llm import LiteLLMProvider


def _png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
        "4e44ae426082"
    )


class _Delta:
    content = ""
    tool_calls = None


class _Choice:
    delta = _Delta()
    finish_reason = "stop"


class _Chunk:
    """Mimics a single LiteLLM stream chunk with a text delta + stop."""
    choices = [_Choice()]
    usage = None


def _fake_completion_factory(captured: dict):
    """Build a callable that mimics litellm.completion for testing."""

    def _completion(**kwargs: Any):
        captured["kwargs"] = kwargs
        captured["messages"] = kwargs.get("messages")
        # Yield a single empty chunk so stream() returns quickly.
        yield _Chunk()

    return _completion


def test_litellm_converts_image_block_to_openai_image_url(tmp_path, monkeypatch):
    """Image attachments survive the LiteLLM conversion path.

    The user attaches a PNG; LiteLLMProvider.stream should hand
    litellm.completion a user message whose content list carries an
    ``image_url`` block with the right data-URI media type, not silently
    drop it.
    """
    import sys
    import types

    # Build a minimal fake litellm module so we don't need the real
    # dependency installed for this regression test.
    fake = types.ModuleType("litellm")
    captured: dict = {}
    fake.completion = _fake_completion_factory(captured)
    fake.suppress_debug_numbers = True
    monkeypatch.setitem(sys.modules, "litellm", fake)

    store = AssetStore(tmp_path)
    aid = store.put(_png(), media_type="image/png", src="t")

    provider = LiteLLMProvider(api_key=None, base_url=None)
    msgs = [{"role": "user",
             "content": [
                 {"type": "text", "text": "what is this?"},
                 {"type": "image", "asset_id": aid},
             ]}]
    list(provider.stream(model="m", system="s", messages=msgs,
                         tools=[], max_tokens=10, asset_store=store))

    sent = captured["messages"]
    # System prepended as a system-role message by _convert_messages.
    user_msg = next(m for m in sent if m["role"] == "user"
                    and isinstance(m["content"], list))
    img_blocks = [b for b in user_msg["content"] if b.get("type") == "image_url"]
    assert img_blocks, (
        "LiteLLM conversion lost the image block — silent data loss "
        f"(sent: {sent})"
    )
    url = img_blocks[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), (
        f"expected data-URI image/png, got {url[:40]!r}")
    # Text block should travel in a separate sibling user message, not
    # be dropped on the floor.
    text_msgs = [m for m in sent if m["role"] == "user"
                 and isinstance(m["content"], str)]
    assert any("what is this" in c for c in (m["content"] for m in text_msgs))


def test_litellm_unknown_asset_id_degrades_to_placeholder(tmp_path, monkeypatch):
    """Unknown asset_id (e.g. file deleted from .assets/) must not cause
    silent drops. _hydrate_messages replaces it with [image: missing],
    which _convert_messages should carry as a plain user text message."""
    import sys
    import types

    fake = types.ModuleType("litellm")
    captured: dict = {}
    fake.completion = _fake_completion_factory(captured)
    fake.suppress_debug_numbers = True
    monkeypatch.setitem(sys.modules, "litellm", fake)

    store = AssetStore(tmp_path)
    provider = LiteLLMProvider(api_key=None, base_url=None)
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": "deadbeefdeadbeef"}]}]
    list(provider.stream(model="m", system="s", messages=msgs,
                         tools=[], max_tokens=10, asset_store=store))

    sent = captured["messages"]
    user_msgs = [m for m in sent if m["role"] == "user"]
    flat = "".join(m["content"] for m in user_msgs
                   if isinstance(m["content"], str))
    assert "[image: missing]" in flat, (
        f"expected placeholder text, got {sent}")


def test_litellm_asset_store_none_falls_back_gracefully(monkeypatch):
    """If LiteLLMProvider is called without an asset_store (e.g. an old
    caller that hasn't been updated), the image block can't hydrate and
    degrades to placeholder text rather than crashing."""
    import sys
    import types

    fake = types.ModuleType("litellm")
    captured: dict = {}
    fake.completion = _fake_completion_factory(captured)
    fake.suppress_debug_numbers = True
    monkeypatch.setitem(sys.modules, "litellm", fake)

    provider = LiteLLMProvider(api_key=None, base_url=None)
    msgs = [{"role": "user",
             "content": [{"type": "image", "asset_id": "abc123def456abcd"}]}]
    list(provider.stream(model="m", system="s", messages=msgs,
                         tools=[], max_tokens=10))  # no asset_store
    flat = "".join(m["content"] for m in captured["messages"]
                   if m["role"] == "user" and isinstance(m["content"], str))
    assert "[image: missing]" in flat
