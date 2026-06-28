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


def _mcp_servers_from_env() -> dict[str, dict] | None:
    """Parse ``MINI_CC_MCP_SERVERS`` as JSON. Expected shape::

        {"docs": {"command": ["npx", "mcp-server-docs"], "env": {...}},
         "fs":   {"command": ["python", "-m", "mcp_server_fs"]}}

    Returns None on missing/empty/invalid input so the rest of the app
    can treat "no MCP configured" as a plain falsy value.
    """
    import json
    raw = os.getenv("MINI_CC_MCP_SERVERS")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Filter out entries that obviously can't boot (no command, command
    # not a non-empty list). We don't validate executables here — that
    # happens lazily at connect time so a broken server doesn't block
    # the whole project from loading.
    out: dict[str, dict] = {}
    for name, spec in parsed.items():
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        if isinstance(cmd, list) and cmd:
            out[name] = {
                "command": cmd,
                **({"env": spec["env"]} if isinstance(spec.get("env"), dict) else {}),
                **({"cwd": spec["cwd"]} if isinstance(spec.get("cwd"), str) else {}),
            }
    return out or None


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
    # MCP servers to launch automatically per project. Each entry is
    # ``{command: [...], env: {...}, cwd: "..."}``. Sourced from
    # ``MINI_CC_MCP_SERVERS`` as JSON, or set programmatically.
    mcp_servers: dict[str, dict] | None = None

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
            mcp_servers=_mcp_servers_from_env(),
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

    def has_llm_credentials(self) -> bool:
        """P0-3: True when the credentials needed for the selected
        primary provider are set. Used by /health and startup warning
        so misconfigured deployments aren't silently broken (the old
        /health returned {ok:true} no matter what)."""
        if "/" in self.primary_model:
            return bool(self.litellm_api_key)
        return bool(self.api_key)


_DEFAULT: Optional[AnthropicConfig] = None


def default_config() -> AnthropicConfig:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AnthropicConfig.from_env()
    return _DEFAULT


def set_default_config(cfg: AnthropicConfig) -> None:
    global _DEFAULT
    _DEFAULT = cfg
