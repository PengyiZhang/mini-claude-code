"""End-to-end: vision-gated image input.

When MINI_CC_MODEL_VISION=false and an MCP vision tool is connected,
the loop must:
1. Inject the vision-tool system-prompt section.
2. Swap each image block for a signed-URL text block instead of
   passing base64 to the model.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_cc.assets import AssetStore
from mini_cc.config import AnthropicConfig, set_default_config
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.mcp.client import MCPPool
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage


def _png() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c630001000000050001009c0d0a0e000000004945"
        "4e44ae426082"
    )


class _Usage:
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Final:
    content = [SimpleNamespace(type="text", text="ok")]
    stop_reason = "end_turn"
    usage = _Usage()


class _Stream:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(())
    def get_final_message(self): return _Final()


class _CaptureProvider:
    """Records the last system prompt + messages passed to stream()."""
    provider_name = "test-capture"

    def __init__(self):
        self.captured_system = None
        self.captured_messages = None
        self.captured_vision_capable = None
        self.captured_minter = None

    def stream(self, *, model, system, messages, tools, max_tokens,
               asset_store=None, vision_capable=True,
               asset_url_minter=None):
        from mini_cc.core.llm import StreamEvent, _hydrate_messages
        self.captured_system = system
        # Run the same hydration the real provider runs, so the test
        # observes the post-swap content (not the pre-hydration raw
        # transcript).
        self.captured_messages = _hydrate_messages(
            messages, asset_store,
            vision_capable=vision_capable,
            asset_url_minter=asset_url_minter)
        self.captured_vision_capable = vision_capable
        self.captured_minter = asset_url_minter
        yield StreamEvent(
            kind="message_stop",
            stop_reason="end_turn",
            content_blocks=[{"type": "text", "text": "ok"}],
            usage={"input_tokens": 0, "output_tokens": 0,
                   "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": 0},
        )


class _FakeMCPClient:
    """For MCPPool — exposes one vision-ish tool."""
    def __init__(self, tools):
        self.tools = tools

    def call_tool(self, name, args):
        return ""


@pytest.fixture
def storage_root(tmp_path):
    yield tmp_path


def _build_loop(storage_root, *, model_vision, vision_tool=False,
                public_base_url="http://test.invalid"):
    """Spin up a loop with an attached asset + optional vision MCP."""
    assets = AssetStore(storage_root / "assets")
    aid = assets.put(_png(), media_type="image/png", src="t")

    cfg = AnthropicConfig(
        api_key=None, base_url=None,
        primary_model="claude-test",
        model_vision=model_vision,
        public_base_url=public_base_url if not model_vision else None,
    )
    set_default_config(cfg)

    pool = None
    if vision_tool:
        pool = MCPPool(project_id="p1")
        pool._clients["zai-mcp-server"] = _FakeMCPClient([
            {"name": "analyze_image",
             "description": "Look at an image",
             "inputSchema": {"type": "object",
                             "properties": {"image": {"type": "string"}}}},
        ])

    provider = _CaptureProvider()
    ref = ProjectRef(
        project_id="p1", project_root=".",
        sandbox=SubprocessSandbox("p1", "."),
        storage=FSStorage(storage_root / "store"),
        tenant_id="t1",
        mcp_pool=pool,
        assets=assets,
        client_factory=lambda: provider,
    )
    loop = AgentLoop(ref, "sess-1")
    loop._user_input_with_image = [
        {"type": "text", "text": "what's in this?"},
        {"type": "image", "asset_id": aid},
    ]
    return loop, provider


def test_vision_disabled_injects_hint_and_swaps_image(storage_root):
    """The defining case: model_vision=False + vision MCP tool
    configured → system prompt has the section, message has text block
    with a signed URL."""
    loop, provider = _build_loop(storage_root, model_vision=False,
                                 vision_tool=True)
    events = list(loop.run(loop._user_input_with_image))
    assert provider.captured_vision_capable is False
    assert provider.captured_minter is not None
    # System prompt has vision hint
    assert "vision tool" in provider.captured_system.lower()
    assert "mcp__zai-mcp-server__analyze_image" in provider.captured_system
    # Message: image block swapped for text block with URL
    user_blocks = provider.captured_messages[-1]["content"]
    text_blocks = [b for b in user_blocks if b.get("type") == "text"]
    image_blocks = [b for b in user_blocks if b.get("type") == "image"]
    assert image_blocks == [], "image block should be swapped for text"
    joined = "\n".join(b["text"] for b in text_blocks)
    assert "/shared/asset/sh_" in joined


def test_vision_enabled_keeps_image_block(storage_root):
    """Backwards compat: default behavior — image stays a base64 block."""
    loop, provider = _build_loop(storage_root, model_vision=True,
                                 vision_tool=True)
    list(loop.run(loop._user_input_with_image))
    assert provider.captured_vision_capable is True
    user_blocks = provider.captured_messages[-1]["content"]
    image_blocks = [b for b in user_blocks if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert "vision tool" not in provider.captured_system.lower()


def test_vision_disabled_no_mcp_falls_back_to_image(storage_root):
    """If model_vision=False but no vision MCP is configured, no URL
    swap (minter declines → fallback to base64)."""
    loop, provider = _build_loop(storage_root, model_vision=False,
                                 vision_tool=False)
    list(loop.run(loop._user_input_with_image))
    # Without a vision MCP, the prompt has no hint.
    assert "vision tool" not in (provider.captured_system or "").lower()


def test_vision_disabled_without_public_base_url_falls_back(storage_root,
                                                             monkeypatch):
    """model_vision=False but no MINI_CC_PUBLIC_BASE_URL → minter
    returns None → image stays as base64. Deployment must set the URL
    or the feature silently no-ops."""
    loop, provider = _build_loop(storage_root, model_vision=False,
                                 vision_tool=True,
                                 public_base_url=None)
    list(loop.run(loop._user_input_with_image))
    user_blocks = provider.captured_messages[-1]["content"]
    image_blocks = [b for b in user_blocks if b.get("type") == "image"]
    assert len(image_blocks) == 1
