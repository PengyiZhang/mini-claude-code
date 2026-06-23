"""Global configuration and LLM provider construction.

Two backends are supported:

- **Anthropic SDK** (default) — for native Anthropic accounts or any
  endpoint that exposes Anthropic's Messages API (DeepSeek's
  ``/anthropic`` adapter, GLM, Kimi, MiniMax, etc.). Configure with
  ``ANTHROPIC_API_KEY`` + ``ANTHROPIC_BASE_URL`` + ``MODEL_ID``.

- **litellm** — for any OpenAI-compatible endpoint (OpenAI itself,
  DeepSeek's native ``/v1`` API, Qwen, Groq, Gemini, etc.). Configure
  with ``LITELLM_API_KEY`` (or ``OPENAI_API_KEY``) + ``LITELLM_BASE_URL``
  (or ``OPENAI_BASE_URL``) and a model name with a provider prefix
  (e.g. ``openai/gpt-4o-mini``, ``deepseek/deepseek-chat``).

Provider selection happens automatically from the model name's prefix
— see :func:`mini_cc.core.llm.select_provider`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000


def _env_first(*names: str) -> Optional[str]:
    """Return the first non-empty env var from ``names``, or None."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


@dataclass
class AnthropicConfig:
    """Project-wide LLM configuration.

    Fields are named after the Anthropic SDK for historical reasons;
    they also drive the litellm backend through ``litellm_*``.
    """
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    primary_model: str = "claude-sonnet-4-6"
    fallback_model: Optional[str] = None
    # litellm-specific credentials. Falls back to OPENAI_* so most
    # OpenAI-compatible drop-ins "just work" without renaming env vars.
    litellm_api_key: Optional[str] = None
    litellm_base_url: Optional[str] = None
    # Tavily API key — when set, the web_search tool calls Tavily's
    # /search endpoint; otherwise the tool returns a clear "not
    # configured" error so the agent can fall back to web_fetch.
    tavily_api_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AnthropicConfig":
        return cls(
            api_key=_env_first("ANTHROPIC_API_KEY", "MINI_CC_ANTHROPIC_API_KEY"),
            base_url=_env_first("ANTHROPIC_BASE_URL", "MINI_CC_ANTHROPIC_BASE_URL"),
            primary_model=_env_first("MODEL_ID") or "claude-sonnet-4-6",
            fallback_model=_env_first("FALLBACK_MODEL_ID"),
            litellm_api_key=_env_first(
                "LITELLM_API_KEY", "OPENAI_API_KEY", "MINI_CC_LITELLM_API_KEY"),
            litellm_base_url=_env_first(
                "LITELLM_BASE_URL", "OPENAI_BASE_URL", "MINI_CC_LITELLM_BASE_URL"),
            tavily_api_key=_env_first(
                "TAVILY_API_KEY", "MINI_CC_TAVILY_API_KEY"),
        )

    def build_client(self) -> Anthropic:
        """Return a raw Anthropic SDK client (used by AnthropicProvider).

        Kept as a method (not deprecated) because the Anthropic backend
        needs the SDK directly — only the litellm path bypasses it.
        """
        return Anthropic(api_key=self.api_key, base_url=self.base_url)

    def build_provider(self):
        """Return the provider selected for ``primary_model``.

        Routes to litellm when the model name has a provider prefix
        (``openai/``, ``deepseek/``, etc.), otherwise to the Anthropic
        SDK. Tests can override by mutating ``primary_model`` before
        calling this.
        """
        from .core.llm import select_provider
        return select_provider(self.primary_model, self)


_DEFAULT: Optional[AnthropicConfig] = None


def default_config() -> AnthropicConfig:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AnthropicConfig.from_env()
    return _DEFAULT


def set_default_config(cfg: AnthropicConfig) -> None:
    global _DEFAULT
    _DEFAULT = cfg
