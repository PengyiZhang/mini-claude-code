"""Phase D — scope grammar + duration parser truth table."""
from __future__ import annotations

import pytest

from mini_cc.auth.scope import parse_duration, scope_allows


# ── parse_duration ────────────────────────────────────────────────────────

@pytest.mark.parametrize("input_,expected", [
    (0, 0),
    (60, 60),
    ("0", 0),
    ("60", 60),
    ("3600s", 3600),
    ("60m", 3600),
    ("12h", 43200),
    ("7d", 604800),
    ("7 d", 604800),
    ("  5m ", 300),
])
def test_parse_duration_accepted(input_, expected):
    assert parse_duration(input_) == expected


@pytest.mark.parametrize("bad", [
    -1, "abc", "12x", "", "1.5h", None, [], "1.5",
])
def test_parse_duration_rejected(bad):
    with pytest.raises((ValueError, TypeError)):
        parse_duration(bad)  # type: ignore[arg-type]


# ── scope_allows ──────────────────────────────────────────────────────────

def test_held_star_matches_anything():
    assert scope_allows(["*"], "sessions:write", "POST") is True
    assert scope_allows(["*"], "files:read", "GET") is True
    assert scope_allows(["*"], "anything:whatever", "DELETE") is True


def test_empty_held_never_matches():
    assert scope_allows([], "sessions:read", "GET") is False
    assert scope_allows(None, "sessions:read", "GET") is False  # type: ignore[arg-type]


def test_read_star_matches_gets_only():
    assert scope_allows(["read:*"], "sessions:read", "GET") is True
    assert scope_allows(["read:*"], "projects:read", "GET") is True
    # GET route, required is resource-only (defaults to method's verb = read)
    assert scope_allows(["read:*"], "sessions", "GET") is True
    # POST (write) → reject
    assert scope_allows(["read:*"], "sessions:write", "POST") is False


def test_write_star_matches_non_gets():
    assert scope_allows(["write:*"], "sessions:write", "POST") is True
    assert scope_allows(["write:*"], "files:write", "DELETE") is True
    # GET (read) → reject
    assert scope_allows(["write:*"], "files:read", "GET") is False


def test_resource_star_scope_matches_any_verb_on_resource():
    # sessions:* matches any method on sessions
    assert scope_allows(["sessions:*"], "sessions:read", "GET") is True
    assert scope_allows(["sessions:*"], "sessions:write", "POST") is True
    assert scope_allows(["sessions:*"], "sessions:write", "DELETE") is True
    # Different resource → reject
    assert scope_allows(["sessions:*"], "files:read", "GET") is False


def test_specific_resource_verb():
    assert scope_allows(["sessions:write"], "sessions:write", "POST") is True
    assert scope_allows(["sessions:write"], "sessions:write", "DELETE") is True
    # Verb mismatch
    assert scope_allows(["sessions:write"], "sessions:read", "GET") is False
    # Resource mismatch
    assert scope_allows(["sessions:write"], "files:write", "POST") is False


def test_or_semantics_across_held():
    held = ["read:*", "sessions:write"]
    # First scope covers GETs
    assert scope_allows(held, "files:read", "GET") is True
    # Second covers sessions writes
    assert scope_allows(held, "sessions:write", "POST") is True
    # Neither covers files writes
    assert scope_allows(held, "files:write", "POST") is False


def test_required_without_verb_defaults_to_method_verb():
    # required="foo" → treated as "foo:<method verb>"
    assert scope_allows(["read:*"], "files", "GET") is True
    assert scope_allows(["write:*"], "files", "POST") is True
    assert scope_allows(["read:*"], "files", "POST") is False


def test_verb_mapping_is_case_insensitive_on_method():
    assert scope_allows(["read:*"], "x:read", "get") is True
    assert scope_allows(["read:*"], "x:read", "GET") is True
