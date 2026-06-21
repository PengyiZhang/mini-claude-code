"""Scope grammar + duration parser for the auth layer.

Scopes are colon-joined `resource:verb` strings:

- `*`                          matches anything
- `read:*`                     matches any GET
- `write:*`                    matches any non-GET
- `sessions:*`                 matches any method on /sessions/...
- `sessions:read`              matches GET on /sessions/...
- `sessions:write`             matches POST/DELETE/PUT/PATCH on /sessions/...
- `projects:write`             matches POST/DELETE on /projects/...

`read` ≡ GET, `write` ≡ everything else. The mapping is fixed at the
HTTP layer so routes don't have to specify the verb explicitly.
"""
from __future__ import annotations

import re

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str | int) -> int:
    """Parse a human-friendly duration into seconds.

    Formats:
    - int (or digit-only str) → seconds
    - "3600s" / "60m" / "12h" / "7d"

    Raises ValueError on bad input.
    """
    if isinstance(s, int):
        if s < 0:
            raise ValueError("duration must be non-negative")
        return s
    if isinstance(s, str) and s.isdigit():
        return int(s)
    if not isinstance(s, str):
        raise ValueError(f"duration must be str or int, got {type(s).__name__}")
    m = _DURATION.match(s)
    if not m:
        raise ValueError(f"unrecognized duration: {s!r}")
    n = int(m.group(1))
    unit = m.group(2)
    return n * _UNIT_SECONDS[unit]


_VERBS = {"read", "write"}


def _verb_for_method(method: str) -> str:
    return "read" if method.upper() == "GET" else "write"


def scope_allows(held: list[str], required: str, method: str) -> bool:
    """Does any scope in ``held`` satisfy ``required`` for HTTP ``method``?

    Grammar:

    - ``*``                      — anything
    - ``read:*`` / ``write:*``   — verb-prefixed wildcard: any resource,
                                   verb-restricted
    - ``<resource>:*``           — any method on the resource
    - ``<resource>:<verb>``      — specific verb on the resource
    - ``<resource>``             — shorthand for ``<resource>:*``

    Verbs map: ``read`` ≡ GET, ``write`` ≡ everything else. The
    required string follows the same grammar (verb typically omitted
    in route declarations, defaulting to the method's verb).
    """
    if not held:
        return False
    verb = _verb_for_method(method)

    # Resolve the required side into (resource, effective_verb).
    if ":" in required:
        req_res, _, req_verb = required.partition(":")
    else:
        req_res, req_verb = required, "*"
    # If required is "foo:read" (explicit verb), honour it; otherwise
    # (foo:* or just foo) substitute the method's verb.
    effective_req_verb = verb if req_verb == "*" else req_verb

    for h in held:
        if h == "*":
            return True
        if ":" in h:
            h_left, _, h_right = h.partition(":")
        else:
            h_left, h_right = h, "*"

        # Case 1: verb-prefixed wildcard (e.g. "read:*", "write:*").
        # Here the left side IS a verb, not a resource.
        if h_left in _VERBS and h_right == "*":
            if effective_req_verb == h_left:
                return True
            continue

        # Case 2: resource-scoped. h_left is the resource.
        if h_left == req_res:
            if h_right == "*" or h_right == effective_req_verb:
                return True
            continue

        # Case 3: full-wildcard resource ("*:read", "*:*"). Rare but
        # supported for symmetry.
        if h_left == "*":
            if h_right == "*" or h_right == effective_req_verb:
                return True
    return False
