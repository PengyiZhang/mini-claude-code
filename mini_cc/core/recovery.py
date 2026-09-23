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


@dataclass(frozen=True)
class ErrorClass:
    kind: str      # rate_limit|overloaded|network|quota|auth|invalid_request|unknown
    transient: bool


_TRANSIENT_KINDS = frozenset({"rate_limit", "overloaded", "network"})


def classify_error(e: Exception) -> ErrorClass:
    """Classify an exception for retry policy + error-event typing.

    Ordering matters: rate_limit/overloaded are matched before the
    permanent kinds because provider rate-limit messages often contain
    words like "quota" — a 429 must stay retryable (pre-existing
    behavior).
    """
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "ratelimit" in name or "429" in msg:
        kind = "rate_limit"
    elif "overloaded" in name or "529" in msg or "overloaded" in msg:
        kind = "overloaded"
    elif ("quota" in msg or "credit" in msg or "balance" in msg
          or "402" in msg):
        kind = "quota"
    elif ("invalid_api_key" in msg or "authentication" in msg
          or "api key" in msg or "permission" in msg or "401" in msg
          or "403" in msg):
        kind = "auth"
    elif ("invalid_request" in msg or "not_found_error" in msg
          or "modelnotfound" in msg or "400" in msg):
        kind = "invalid_request"
    elif ("timeout" in name or "timed out" in msg or "connection" in msg
          or "eof" in msg or "reset" in msg):
        kind = "network"
    else:
        kind = "unknown"
    return ErrorClass(kind=kind, transient=kind in _TRANSIENT_KINDS)


def with_retry(fn: Callable[[], T], state: RecoveryState,
               max_retries: int = 3, on_event=None) -> T:
    cfg = default_config()
    for attempt in range(max_retries):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            cls = classify_error(e)
            if not cls.transient:
                # M4-2: quota/auth/invalid_request 类永久错误——重试无意义，
                # 快速失败并附分类事件，让上层与客户端能看到原因类别。
                if on_event:
                    on_event({"type": "retry_aborted", "reason": cls.kind,
                              "error": str(e)})
                raise
            if cls.kind == "rate_limit":
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "429",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            if cls.kind == "overloaded":
                state.consecutive_529 += 1
                cfg_fallback = cfg.fallback_model
                if (state.consecutive_529 >= 2 and cfg_fallback
                        and state.current_model != cfg_fallback):
                    state.current_model = cfg_fallback
                    state.consecutive_529 = 0
                    if on_event:
                        on_event({"type": "fallback_model",
                                  "model": cfg_fallback})
                d = retry_delay(attempt)
                if on_event:
                    on_event({"type": "retry", "reason": "529",
                              "attempt": attempt + 1, "delay": d})
                time.sleep(d)
                continue
            # network（瞬态）——通用退避
            d = retry_delay(attempt)
            if on_event:
                on_event({"type": "retry", "reason": cls.kind,
                          "attempt": attempt + 1, "delay": d})
            time.sleep(d)
            continue
    raise RuntimeError(f"Max retries ({max_retries}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)
