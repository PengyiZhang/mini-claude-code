"""Parse `@name` mentions out of user input and route them to teammates.

Why this lives outside the LLM tool layer: before this module existed,
the lead had to coax the model into calling `send_message` to forward an
`@alice` mention. That works on strong agentic models ~60% of the time
and never on weaker ones, which made the contract non-deterministic.

Routing at agent-loop entry makes the contract deterministic: if the
user typed `@alice …`, alice gets a copy in her inbox, full stop. The
LLM is still free to call send_message for richer follow-ups (this
keeps the bus symmetric) but mention routing is no longer model-gated.
"""
from __future__ import annotations

import re
from typing import Iterable

from . import MessageBus

# A mention is `@` preceded by start-of-string or whitespace, followed
# by a name made of word chars (incl. underscore/dash/dot) or any CJK
# char. The boundary guard rejects email-style `foo@bar`. CJK is
# allowed because the spawner accepts those characters in teammate
# names — we don't want to silently fail to route a perfectly legal
# `@张三` mention.
_NAME_CHARS = r"A-Za-z0-9_.\-一-鿿"
_MENTION_RE = re.compile(
    rf"(?:^|\s)(@[{_NAME_CHARS}]+)"
)


def parse_mentions(text: str) -> list[str]:
    """Extract unique `@name` mentions in order of first appearance.

    Returns the name list (without the leading `@`). A bare `@` with no
    name after it yields nothing. Email-style `foo@bar` (no leading
    boundary) is not a mention. Repeats are deduped preserving order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _MENTION_RE.finditer(text):
        token = m.group(1)  # the "@name" slice
        name = token[1:]
        if not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def route_mentions(
    bus: MessageBus,
    from_agent: str,
    text: str,
    original_text: str | None = None,
) -> list[str]:
    """Deliver a copy of the user's message to every mentioned teammate.

    - The full ``original_text`` (defaults to ``text``) is sent, not
      just the mention token — the teammate needs the surrounding
      context to act on the ping.
    - Self-mentions (`@<from_agent>`) are silently skipped; otherwise
      the lead would echo messages to its own inbox on every drain.
    - Repeats are deduped (one copy per recipient).
    - Returns the ordered recipient list so callers can log/emit a
      routing event.

    The recipient list is determined purely from text parsing — we do
    NOT consult the spawner to check whether the named teammate exists.
    A typo'd `@alicia` simply yields an unread message in the
    `alicia.jsonl` file; that's by design: the message is still
    recoverable later (e.g. if `alicia` is spawned after the fact), and
    rejecting unknown names here would couple this module to the
    spawner's lifecycle.
    """
    content = original_text if original_text is not None else text
    recipients: list[str] = []
    for name in parse_mentions(text):
        if name == from_agent:
            continue
        bus.send(
            from_agent=from_agent,
            to_agent=name,
            content=content,
            msg_type="mention",
            metadata={
                "mention": True,
                "routed_from": from_agent,
            },
        )
        recipients.append(name)
    return recipients


__all__: Iterable[str] = ("parse_mentions", "route_mentions")
