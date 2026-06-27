"""F3.1: /search — global full-text search across sessions.

Substring search across every session's messages within a project.
Returns top-N matches with role + snippet for the `/search` command
and the Web UI global search bar.

Implementation is intentionally simple (no FTS index) — adequate for
moderate scale. The function signature is FTS-ready: swap the body for
a sqlite-FTS5 backed lookup later without touching callers.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _seed(storage, project_id: str, sessions: dict[str, list[dict]]):
    for sid, msgs in sessions.items():
        storage.save_messages(project_id, sid, msgs)


def test_search_finds_user_text_match(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {
        "sess_a": [{"role": "user", "content": "fix the login bug"},
                   {"role": "assistant", "content": "ok"}],
        "sess_b": [{"role": "user", "content": "unrelated topic"},
                   {"role": "assistant", "content": "ok"}],
    })
    matches = s.search_messages("p1", "login bug")
    assert len(matches) == 1
    assert matches[0].session_id == "sess_a"
    assert matches[0].role == "user"
    assert "login bug" in matches[0].snippet


def test_search_finds_assistant_text_match(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {
        "sess_a": [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "Let me check the database."}],
    })
    matches = s.search_messages("p1", "database")
    assert matches[0].role == "assistant"


def test_search_is_case_insensitive(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {
        "sess_a": [{"role": "user", "content": "POSTGRES is great"}],
    })
    matches = s.search_messages("p1", "postgres")
    assert len(matches) == 1


def test_search_returns_empty_when_no_match(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {"sess_a": [{"role": "user", "content": "hello"}]})
    assert s.search_messages("p1", "missing") == []


def test_search_respects_limit(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {
        f"sess_{i}": [{"role": "user", "content": f"findme {i}"}]
        for i in range(10)
    })
    matches = s.search_messages("p1", "findme", limit=3)
    assert len(matches) == 3


def test_search_snippet_is_windowed(tmp_path):
    """Long messages get a windowed snippet around the match, not the
    full text — keeps the result list readable."""
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    long_body = "x" * 200 + " NEEDLE " + "y" * 200
    _seed(s, "p1", {"sess_a": [{"role": "user", "content": long_body}]})
    matches = s.search_messages("p1", "NEEDLE")
    snip = matches[0].snippet
    assert "NEEDLE" in snip
    assert len(snip) < len(long_body)  # windowed


def test_search_handles_empty_storage(tmp_path):
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    assert s.search_messages("never-created", "q") == []


def test_search_skips_unrelated_projects(tmp_path):
    """Per-project isolation: p1's search must NOT see p2's sessions."""
    from mini_cc.storage.fs import FSStorage
    s = FSStorage(tmp_path)
    _seed(s, "p1", {"sess_a": [{"role": "user", "content": "shared keyword"}]})
    _seed(s, "p2", {"sess_b": [{"role": "user", "content": "shared keyword"}]})
    matches = s.search_messages("p1", "shared")
    assert all(m.session_id == "sess_a" for m in matches)
