"""Context compaction. Pure functions over message lists.

Ports the layered scheme from s20 (1055-1204):
- tool_result_budget: keep only N most-recent tool results full, truncate rest
- snip_compact: drop old tool_use/tool_result pairs entirely
- micro_compact: drop trailing user messages with no tool info
- estimate_size: rough token estimate via character count / 4
"""
from __future__ import annotations

import json
from typing import Callable, Iterable

CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
TRUNCATE_AFTER = 2000  # truncated tool results shrink to this many chars


def estimate_size(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    n += len(json.dumps(b, ensure_ascii=False, default=str))
                else:
                    n += len(str(b))
    return n // 4  # ~4 chars per token


def _is_tool_result_block(b) -> bool:
    return isinstance(b, dict) and b.get("type") == "tool_result"


def tool_result_budget(messages: list[dict],
                       keep: int = KEEP_RECENT_TOOL_RESULTS) -> list[dict]:
    """Truncate tool_result content older than the most-recent `keep`."""
    tool_use_ids: list[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_use_ids.append(b.get("id", ""))
    recent = set(tool_use_ids[-keep:]) if keep > 0 else set()

    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_content = []
        for b in content:
            if _is_tool_result_block(b) and b.get("tool_use_id") not in recent:
                c = b.get("content", "")
                if isinstance(c, str) and len(c) > TRUNCATE_AFTER:
                    nb = dict(b)
                    nb["content"] = c[:TRUNCATE_AFTER] + "\n[...truncated by compaction]"
                    new_content.append(nb)
                    continue
            new_content.append(b)
        out.append({**m, "content": new_content})
    return out


def snip_compact(messages: list[dict]) -> list[dict]:
    """Drop entire tool_use/tool_result pairs from the oldest assistant turn
    that has them, when context is over budget."""
    if estimate_size(messages) < CONTEXT_LIMIT:
        return messages
    for i, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
            new_content = [{"type": "text",
                            "text": "[earlier tool calls snipped by compaction]"}]
            out = list(messages)
            out[i] = {**m, "content": new_content}
            return out
    return messages


def micro_compact(messages: list[dict]) -> list[dict]:
    """Collapse trailing empty user messages."""
    out = list(messages)
    while (len(out) >= 2 and out[-1].get("role") == "user"
           and out[-2].get("role") == "user"):
        last = out[-1].get("content")
        if isinstance(last, list) and len(last) == 1:
            if (isinstance(last[0], dict)
                    and last[0].get("type") == "tool_result"
                    and not str(last[0].get("content", "")).strip()):
                out.pop()
                continue
        if isinstance(last, str) and not last.strip():
            out.pop()
            continue
        break
    return out


def compact_history(messages: list[dict], keep_recent: int = 6,
                    before_compact: Callable[[list[dict]], None] | None = None
                    ) -> list[dict]:
    """Aggressive: replace everything but the last `keep_recent` messages
    with a single summary placeholder. (LLM-driven summarization is a later
    enhancement; for now we keep the recency window.)

    If `before_compact` is provided, it is called with the full message
    list *before* the recency window is applied — giving the caller a
    chance to persist a transcript of the discarded content (ports
    s20's write_transcript-on-compact behavior).
    """
    if len(messages) <= keep_recent:
        return messages
    if before_compact is not None:
        before_compact(messages)
    summary = ("[Earlier conversation compacted. "
               f"{len(messages) - keep_recent} messages summarized.]")
    return [{"role": "user", "content": summary}, *messages[-keep_recent:]]


def prepare_context(messages: list[dict],
                    before_compact: Callable[[list[dict]], None] | None = None
                    ) -> list[dict]:
    """Apply the layered compaction pipeline (in place).

    `before_compact` is forwarded to compact_history if it fires.
    """
    messages[:] = tool_result_budget(messages)
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        messages[:] = compact_history(messages, before_compact=before_compact)
    return messages
