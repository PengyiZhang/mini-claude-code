"""Card event schema for slash-command output.

A CardEvent is a structured alternative to the legacy ``{"type":"text", ...}``
event that slash-command handlers emit. The frontend ``CardView`` component
dispatches on ``variant`` to a dedicated renderer (``CardList``,
``CardKeyValue``, ``CardTable``, ``CardSteps``), giving each command a
typed, interactive surface instead of a plain markdown bubble.

Wire format (Python → frontend):

    {
      "type": "card",
      "id": "agents-roster",            # stable; replace-by-id on re-emit
      "variant": "list",
      "title": "Teammates",             # optional
      "icon": "agents",                 # one of ICON_KEYS; null allowed
      "status": "ok",                   # "ok" | "warning" | "error"
      "error_message": null,
      "payload": { ...variant-specific... },
      "actions": [                      # footer buttons → trigger slash cmd
        {"label": "＋ spawn",
         "command": "/agents spawn",
         "tone": "primary"}
      ],
      "emitted_at": 1782780000.0,       # set by to_dict on first call
      "revision": 1                     # bump on re-emit (live refresh)
    }

Persistence: cards ride inside the Anthropic transcript as synthetic
``tool_use`` blocks with ``name="__card__"`` (see server-side slash bridge
and ``store.ts::rawToChatMessages``). They survive session compaction
because they're just another tool call from the transcript's perspective.

When extending this schema:
  - ADD fields with defaults (don't break old persisted sessions)
  - ADD entries to ICON_KEYS only after the frontend ships the matching glyph
  - DON'T remove or rename without a migration on the hydration side
"""
from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


CardVariant = Literal["list", "table", "key_value", "steps"]
Tone = Literal["default", "ok", "warn", "err", "accent"]
CardStatus = Literal["ok", "warning", "error"]


# Closed enum of icon keys the frontend knows how to render. Add new
# entries here ONLY after the frontend CardIcon component ships the
# matching glyph — otherwise handlers will emit an icon key that
# renders as a fallback box.
ICON_KEYS: frozenset[str] = frozenset({
    "agents", "bg", "loop", "workflow", "config", "help",
    "tools", "mcp", "sessions", "logs", "tasks", "skills", "search",
    "permissions", "cost", "model",
})


@dataclass
class CardBadge:
    """Small colored pill rendered next to an item title or k/v value."""
    text: str
    tone: Tone = "default"


@dataclass
class CardAction:
    """Button rendered in a card footer (or item menu) that triggers a
    slash command via the chat input.

    ``command`` is the literal slash command string (including the leading
    ``/``); the frontend ``runCommand`` store action parses and dispatches
    it through the same path as a manually-typed command.
    """
    label: str
    command: str
    tone: Tone = "default"


@dataclass
class CardListItem:
    """One row in a ``list``-variant card."""
    id: str                              # stable; used for replace-by-id
    title: str
    subtitle: str | None = None
    icon: str | None = None              # one of ICON_KEYS
    badges: list[CardBadge] = field(default_factory=list)
    meta: str | None = None              # right-aligned, e.g. "4m"
    # When set, clicking the row runs this slash command and the
    # resulting card replaces the row body inline (child card). Use
    # for "click teammate → see their inbox" patterns.
    expandable_command: str | None = None
    menu: list[CardAction] = field(default_factory=list)


@dataclass
class CardListPayload:
    items: list[CardListItem]
    empty_hint: str | None = None        # shown when items is empty
    summary: str | None = None           # footer line, e.g. "3 alive · 1 stopped"
    group_by: str | None = None          # future: group items by a key


@dataclass
class CardKeyValuePair:
    k: str
    v: str
    mono: bool = False                   # render in <code>
    sensitive: bool = False              # mask value, show on click
    badge: CardBadge | None = None


@dataclass
class CardKeyValuePayload:
    pairs: list[CardKeyValuePair]


@dataclass
class CardEvent:
    """Top-level card event. Handlers build this and call ``to_dict`` to
    get the SSE wire dict; the dict is what gets yielded from the
    generator AND what gets persisted in the transcript.
    """
    id: str                              # stable; e.g. "agents-roster", "config"
    variant: CardVariant
    title: str | None = None
    icon: str | None = None              # one of ICON_KEYS (or None)
    status: CardStatus = "ok"
    error_message: str | None = None
    # variant-specific payload. Typed as a plain dict on the wire; each
    # variant has a ``XxxPayload`` dataclass above to make construction
    # type-safe (call ``dataclasses.asdict(...)`` to convert to the wire
    # dict before assigning here).
    payload: dict[str, Any] = field(default_factory=dict)
    actions: list[CardAction] = field(default_factory=list)
    # Set lazily by to_dict on first call. Public so tests can override.
    emitted_at: float = 0.0
    revision: int = 1


def to_dict(ev: CardEvent) -> dict[str, Any]:
    """Serialize a CardEvent to the SSE wire format.

    Stamps ``emitted_at`` on first call if the caller didn't set it.
    Subsequent calls return the same timestamp so SSE ordering on the
    client stays consistent across re-emissions.
    """
    if not ev.emitted_at:
        ev.emitted_at = time.time()
    d = dataclasses.asdict(ev)
    d["type"] = "card"
    return d


def persist_card_event(messages: list[dict], card: dict) -> None:
    """Append a card event to the Anthropic transcript as a synthetic
    ``tool_use{name="__card__"}`` block paired with a ``tool_result``.

    Why this shape: cards need to survive session compaction, but they
    aren't real tool calls. Reusing the tool_use/tool_result envelope
    means the existing persistence + hydration path picks them up for
    free (the transcript treats them like any other tool call), and the
    frontend's ``rawToChatMessages`` routes them to ``msg.cards`` instead
    of ``msg.activities`` by matching on ``name == "__card__"``.

    The wire-level ``type:"card"`` field is stripped before persistence
    (it's SSE metadata, not part of the card shape).

    Mutates ``messages`` in place; callers are responsible for calling
    ``save_messages`` afterwards.
    """
    tid = f"card_{uuid.uuid4().hex[:8]}"
    payload = {k: v for k, v in card.items() if k != "type"}
    messages.append({
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": tid,
            "name": "__card__",
            "input": payload,
        }],
    })
    messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tid,
            "content": "ok",
        }],
    })
