"""AnthropicConfig.from_env env-var resolution.

Regression for the "Could not resolve authentication method" bug: the README
Web UI section documented the env vars as ``MINI_CC_ANTHROPIC_API_KEY`` /
``MINI_CC_ANTHROPIC_BASE_URL``, but the code only read ``ANTHROPIC_API_KEY``
/ ``ANTHROPIC_BASE_URL``. Users who copy-pasted the README ended up with
``api_key=None`` and the anthropic SDK raised a TypeError on the first
chat turn.

Fix: accept both prefixes. The bare ``ANTHROPIC_*`` form wins so users who
already use the SDK convention aren't surprised.
"""
from __future__ import annotations

import pytest

from mini_cc.config import AnthropicConfig


@pytest.fixture
def clean_anthropic_env(monkeypatch):
    """Strip every env var from_env() cares about so tests are independent."""
    for k in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "MINI_CC_ANTHROPIC_API_KEY",
        "MINI_CC_ANTHROPIC_BASE_URL",
        "MODEL_ID",
        "FALLBACK_MODEL_ID",
    ):
        monkeypatch.delenv(k, raising=False)


def test_bare_anthropic_api_key_is_read(clean_anthropic_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bare")
    cfg = AnthropicConfig.from_env()
    assert cfg.api_key == "sk-bare"


def test_mini_cc_prefixed_api_key_is_accepted_as_fallback(clean_anthropic_env, monkeypatch):
    """If only the README-documented MINI_CC_ANTHROPIC_API_KEY is set,
    from_env should still resolve a key (was the original bug)."""
    monkeypatch.setenv("MINI_CC_ANTHROPIC_API_KEY", "sk-prefixed")
    cfg = AnthropicConfig.from_env()
    assert cfg.api_key == "sk-prefixed"


def test_bare_anthropic_base_url_is_read(clean_anthropic_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    cfg = AnthropicConfig.from_env()
    assert cfg.base_url == "https://proxy.example.com"


def test_mini_cc_prefixed_base_url_is_accepted_as_fallback(clean_anthropic_env, monkeypatch):
    monkeypatch.setenv("MINI_CC_ANTHROPIC_BASE_URL", "https://deepseek.example.com/anthropic")
    cfg = AnthropicConfig.from_env()
    assert cfg.base_url == "https://deepseek.example.com/anthropic"


def test_bare_prefix_wins_over_mini_cc(clean_anthropic_env, monkeypatch):
    """When both are set, the canonical SDK form wins (least surprise)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bare")
    monkeypatch.setenv("MINI_CC_ANTHROPIC_API_KEY", "sk-prefixed")
    cfg = AnthropicConfig.from_env()
    assert cfg.api_key == "sk-bare"


def test_no_api_key_returns_none(clean_anthropic_env):
    """When neither env var is set, api_key is None — AgentLoop.client
    relies on this to raise a clear error instead of silently using a
    half-configured client."""
    cfg = AnthropicConfig.from_env()
    assert cfg.api_key is None
    assert cfg.base_url is None
