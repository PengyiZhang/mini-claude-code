"""Configuration slash commands (/config, /output-style) (split from commands/registry.py — M3-1)."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterator, Optional

from ...config import default_config
from ...core.llm import looks_like_litellm
from ..cards import (
    CardAction,
    CardBadge,
    CardEvent,
    CardKeyValuePair,
    CardKeyValuePayload,
    CardListItem,
    CardListPayload,
    to_dict,
)
from ..registry import CommandContext, SlashCommand

from ._util import _loop_of

def _cmd_config(ctx: CommandContext) -> Iterator[dict]:
    """Show current effective configuration as a key_value card.

    API keys are flagged ``sensitive`` so the frontend masks them by
    default; a click reveals the value. Useful for debugging "why isn't
    the model connecting" without leaking secrets into chat history.
    """
    cfg = default_config()
    backend = "litellm" if looks_like_litellm(cfg.primary_model) else "anthropic"
    sess_loop = _loop_of(ctx)
    style = getattr(getattr(sess_loop, "state", None), "output_style", None) or "default"

    def kv(k: str, v: str, *, mono: bool = False, sensitive: bool = False) -> CardKeyValuePair:
        return CardKeyValuePair(k=k, v=v, mono=mono, sensitive=sensitive)

    pairs: list[CardKeyValuePair] = [
        kv("primary_model", cfg.primary_model, mono=True),
        kv("fallback_model", cfg.fallback_model or "—", mono=True),
        kv("active backend", backend, mono=True),
        kv("anthropic_base_url", cfg.base_url or "— (default SDK)", mono=True),
        kv("litellm_base_url", cfg.litellm_base_url or "—", mono=True),
        kv("anthropic_api_key", _redact(cfg.api_key), mono=True, sensitive=True),
        kv("litellm_api_key", _redact(cfg.litellm_api_key), mono=True, sensitive=True),
        kv("tavily_api_key", _redact(cfg.tavily_api_key), mono=True, sensitive=True),
        kv("output_style", style, mono=True),
    ]
    card = CardEvent(
        id="config",
        variant="key_value",
        title="Configuration",
        icon="config",
        status="ok",
        payload=CardKeyValuePayload(pairs=pairs).__dict__,
        actions=[
            CardAction(label="↻ reload", command="/config", tone="default"),
        ],
    )
    yield to_dict(card)
    yield {"type": "done"}


def _redact(secret: str | None) -> str:
    """Show '—' for None, '••••last4' for short keys, full prefix for long."""
    if not secret:
        return "—"
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


# Valid output-style values. Keep in sync with loop.state.output_style docs.


_OUTPUT_STYLES = ("default", "terse", "detailed", "streamlined")


def _cmd_output_style(ctx: CommandContext) -> Iterator[dict]:
    """Show or set the output-style hint on the active session.

    The hint is stored on ``loop.state.output_style`` and read by the
    system-prompt builder to shape the model's verbosity. With no args
    it shows the current style; with one of the valid values it sets it.
    """
    args = (ctx.args or "").strip()
    sess_loop = _loop_of(ctx)
    if sess_loop is None:
        yield {"type": "error", "message": "session not warm"}
        return
    state = getattr(sess_loop, "state", None)
    if state is None:
        yield {"type": "error", "message": "session has no state"}
        return

    if not args:
        current = getattr(state, "output_style", None) or "default"
        yield {"type": "text",
               "text": (f"**Output style:** `{current}`\n\n"
                        f"Valid values: {', '.join(_OUTPUT_STYLES)}")}
        yield {"type": "done"}
        return
    style = args.split()[0].lower()
    if style not in _OUTPUT_STYLES:
        yield {"type": "error",
               "message": f"unknown style '{style}'. "
                          f"Choose from: {', '.join(_OUTPUT_STYLES)}"}
        return
    state.output_style = style  # type: ignore[attr-defined]
    yield {"type": "text",
           "text": f"📝 output style set to `{style}`. "
                   f"It will shape the next assistant turn."}
    yield {"type": "done"}



def register(reg) -> None:

    reg.register(SlashCommand(
        name="config",
        description="Show effective configuration (model, providers, API key status).",
        handler=_cmd_config,
    ))
    reg.register(SlashCommand(
        name="output-style",
        description="Show or set the output-style hint (terse / default / detailed / streamlined).",
        handler=_cmd_output_style,
    ))