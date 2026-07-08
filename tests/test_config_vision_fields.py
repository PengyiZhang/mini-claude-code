"""Config: model_vision + public_base_url fields."""
from __future__ import annotations

import pytest

from mini_cc.config import AnthropicConfig


def test_model_vision_defaults_to_true(monkeypatch):
    """Backwards compat: existing deployments without the env var
    continue to embed images directly."""
    monkeypatch.delenv("MINI_CC_MODEL_VISION", raising=False)
    cfg = AnthropicConfig.from_env()
    assert cfg.model_vision is True


def test_model_vision_false_when_env_set_to_false(monkeypatch):
    """Explicit opt-out: ``MINI_CC_MODEL_VISION=false`` switches the
    deployment to the vision-MCP-tool path."""
    monkeypatch.setenv("MINI_CC_MODEL_VISION", "false")
    cfg = AnthropicConfig.from_env()
    assert cfg.model_vision is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "TRUE", "On"])
def test_model_vision_truthy_values(monkeypatch, val):
    monkeypatch.setenv("MINI_CC_MODEL_VISION", val)
    assert AnthropicConfig.from_env().model_vision is True


@pytest.mark.parametrize("val", ["false", "0", "no", "OFF", "anything-else"])
def test_model_vision_falsy_values(monkeypatch, val):
    monkeypatch.setenv("MINI_CC_MODEL_VISION", val)
    assert AnthropicConfig.from_env().model_vision is False


def test_public_base_url_reads_env(monkeypatch):
    monkeypatch.setenv("MINI_CC_PUBLIC_BASE_URL", "http://host:8888")
    cfg = AnthropicConfig.from_env()
    assert cfg.public_base_url == "http://host:8888"


def test_public_base_url_defaults_to_none(monkeypatch):
    monkeypatch.delenv("MINI_CC_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("MINI_CC_PUBLIC_URL", raising=False)
    cfg = AnthropicConfig.from_env()
    assert cfg.public_base_url is None
