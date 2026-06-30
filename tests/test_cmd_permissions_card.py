"""Tests for /permissions slash command emitting a CardEvent."""
from __future__ import annotations

from types import SimpleNamespace

from mini_cc.commands import default_registry
from mini_cc.commands.registry import CommandContext


def _run_permissions(project=None):
    reg = default_registry()
    cmd = reg.resolve("permissions")
    ctx = CommandContext(
        project_id="p",
        session_id="s",
        tenant_id="t",
        args="",
        project=project,
    )
    return list(cmd.handler(ctx))


def _extract_card(events):
    cards = [e for e in events if e.get("type") == "card"]
    return cards[0] if cards else None


def _make_project_with_policy():
    """Build a minimal project stand-in with a sandbox.policy attached."""
    policy = SimpleNamespace(
        blocked=[("rm-rf", r"^rm\s+-rf"),
                 ("sudo", r"^sudo\b")],
        allowed_git={"status", "log", "diff"},
        allowed_env={"PATH", "HOME"},
    )
    sandbox = SimpleNamespace(policy=policy)
    return SimpleNamespace(sandbox=sandbox, tenant_id="t")


def test_permissions_yields_card_event_when_sandbox_present():
    events = _run_permissions(_make_project_with_policy())
    card = _extract_card(events)
    assert card is not None, f"no card event in /permissions output: {events!r}"
    assert card["id"] == "permissions"
    assert card["variant"] == "key_value"
    assert card["icon"] == "permissions"
    assert card["status"] == "ok"


def test_permissions_card_includes_blocked_patterns_as_mono_pairs():
    events = _run_permissions(_make_project_with_policy())
    card = _extract_card(events)
    pairs = card["payload"]["pairs"]
    keys = {p["k"] for p in pairs}
    # Each blocked pattern surfaces as its own mono pair so users can
    # scan them in a fixed grid rather than a wrapped comma list.
    assert "blocked: rm-rf" in keys
    assert "blocked: sudo" in keys
    rm_pair = next(p for p in pairs if p["k"] == "blocked: rm-rf")
    assert rm_pair["mono"] is True


def test_permissions_card_includes_allowed_git_and_env_as_pairs():
    events = _run_permissions(_make_project_with_policy())
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "allowed git" in pairs
    assert "status" in pairs["allowed git"]
    assert "allowed env" in pairs
    assert "PATH" in pairs["allowed env"]


def test_permissions_card_includes_hook_deny_lists():
    """Surface the global DENY_LIST + DESTRUCTIVE hook denies too —
    those answer "why was my command blocked" without grepping source."""
    events = _run_permissions(_make_project_with_policy())
    card = _extract_card(events)
    pairs = {p["k"]: p["v"] for p in card["payload"]["pairs"]}
    assert "hook DENY_LIST" in pairs
    assert "hook DESTRUCTIVE" in pairs


def test_permissions_no_sandbox_yields_text_not_card():
    """When the project has no sandbox (e.g. ephemeral test project),
    don't emit a card with empty pairs — yield a text marker so the
    user can tell 'not configured' from 'configured and empty'."""
    events = _run_permissions(project=None)
    card = _extract_card(events)
    assert card is None
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
    assert "not available" in text.lower() or "no sandbox" in text.lower()
