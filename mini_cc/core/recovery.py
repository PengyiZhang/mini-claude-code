"""Error recovery: retry with exponential backoff and model fallback."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from ..config import default_config

T = TypeVar("T")


@dataclass
class RecoveryState:
    has_escalated: bool = False
    recovery_count: int = 0
    consecutive_529: int = 0
    has_attempted_reactive_compact: bool = False
    current_model: str = ""
    # Output-style hint set by /output-style. The system-prompt builder
    # reads this to shape verbosity. "" means "no override" (model default).
    output_style: str = ""

    def __post_init__(self):
        if not self.current_model:
            self.current_model = default_config().primary_model


def retry_delay(attempt: int, base_ms: int = 500) -> float:
    base = min(base_ms * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn: Callable[[], T], state: RecoveryState,
               max_retries: int = 3, on_event=None) -> T:
    cfg = default_config()
    for attempt in range(max_retries):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__.lower()
            msg = str(e).lower()
            if "ratelimit" in name or "429" in msg:
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "429",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if (state.consecutive_529 >= 2 and cfg.fallback_model
                        and state.current_model != cfg.fallback_model):
                    state.current_model = cfg.fallback_model
                    state.consecutive_529 = 0
                    if on_event:
                        on_event({"type": "fallback_model",
                                  "model": cfg.fallback_model})
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "529",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            raise
    raise RuntimeError(f"Max retries ({max_retries}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)
