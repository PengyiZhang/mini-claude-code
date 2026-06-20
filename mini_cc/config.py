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


@dataclass
class AnthropicConfig:
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    primary_model: str = "claude-sonnet-4-6"
    fallback_model: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AnthropicConfig":
        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
            primary_model=os.getenv("MODEL_ID", "claude-sonnet-4-6"),
            fallback_model=os.getenv("FALLBACK_MODEL_ID") or None,
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
