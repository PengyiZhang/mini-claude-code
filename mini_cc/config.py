"""Global configuration and Anthropic client construction."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    primary_model: str = "claude-sonnet-4-6"
    fallback_model: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AnthropicConfig":
        # ANTHROPIC_* is the canonical name used by the SDK and most docs.
        # MINI_CC_ANTHROPIC_* is accepted as a fallback because earlier
        # README versions (Web UI section) documented that prefix; supporting
        # both avoids a confusing "Could not resolve authentication method"
        # failure on the first chat turn when users copy-paste the README.
        return cls(
            api_key=_env_first("ANTHROPIC_API_KEY", "MINI_CC_ANTHROPIC_API_KEY"),
            base_url=_env_first("ANTHROPIC_BASE_URL", "MINI_CC_ANTHROPIC_BASE_URL"),
            primary_model=_env_first("MODEL_ID") or "claude-sonnet-4-6",
            fallback_model=_env_first("FALLBACK_MODEL_ID"),
        )

    def build_client(self) -> Anthropic:
        return Anthropic(api_key=self.api_key, base_url=self.base_url)


_DEFAULT: Optional[AnthropicConfig] = None


def default_config() -> AnthropicConfig:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AnthropicConfig.from_env()
    return _DEFAULT


def set_default_config(cfg: AnthropicConfig) -> None:
    global _DEFAULT
    _DEFAULT = cfg
