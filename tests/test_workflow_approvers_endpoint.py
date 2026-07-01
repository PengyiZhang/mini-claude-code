"""Task 3b (debug.7.md): candidate-approvers helper for checkpoint UI.

The checkpoint step's ``config.approvers`` is a free-form list of
strings today. The UI has no way to populate the dropdown — users
don't know whether to type a teammate name, an email, or "human".

``list_candidate_approvers(project)`` returns a structured list:
- Active teammates (sourced from the project's TeammateSpawner), with
  ``alive=True`` and ``role`` for the dropdown subtitle.
- Stopped teammates are still listed (so the user can re-pick a known
  name) but flagged ``alive=False``.
- A ``{type: "human"}`` entry for arbitrary humans — the UI shows a
  free-form text input for this.
- A ``{type: "lead"}`` entry for the project's main agent (self-
  approve during dev).

The list is descriptive only — the approver string the route stores
at resolve time is still whatever the caller sends. This just tells
the UI what's available.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from mini_cc.teams import TeammateInfo, TeammateSpawner


class _Project:
    """Minimal project stub — only ``teams`` is consulted."""
    def __init__(self, spawner=None):
        self.teams = spawner


def test_approvers_lists_active_teammates():
    spawner = TeammateSpawner(
        __import__("pathlib").Path("/tmp/x"),
        loop_factory=lambda sid: None)
    spawner._teammates["alice"] = TeammateInfo(
        name="alice", role="reviewer", alive=True,
        started_at=time.time())
    spawner._teammates["bob"] = TeammateInfo(
        name="bob", role="reviewer", alive=False,
        started_at=time.time(), stopped_at=time.time())
    project = _Project(spawner)

    from mini_cc.workflow.approvers import list_candidate_approvers
    candidates = list_candidate_approvers(project)
    by_name = {c["name"]: c for c in candidates}
    assert by_name["alice"]["alive"] is True
    assert by_name["alice"]["role"] == "reviewer"
    assert by_name["alice"]["type"] == "teammate"
    # Stopped teammates surface but flagged.
    assert by_name["bob"]["alive"] is False


def test_approvers_includes_lead_and_human_options():
    """Without any spawner attached, still returns lead + human."""
    project = _Project(None)
    from mini_cc.workflow.approvers import list_candidate_approvers
    candidates = list_candidate_approvers(project)
    types = {c["type"] for c in candidates}
    assert "lead" in types
    assert "human" in types
    # Lead entry has a known name.
    lead = next(c for c in candidates if c["type"] == "lead")
    assert lead["name"] == "lead"


def test_approvers_handles_no_teammates_section():
    """Project without a teams attribute → just lead + human."""
    class _Bare:
        pass
    from mini_cc.workflow.approvers import list_candidate_approvers
    candidates = list_candidate_approvers(_Bare())
    names = {c["name"] for c in candidates}
    assert "lead" in names


def test_approvers_dedups_teammate_named_lead():
    """Edge case: a teammate literally named 'lead' would collide with
    the built-in lead entry. The dedup must keep the teammate and skip
    the synthetic one (or vice versa) — either way, the call must not
    raise and the list must contain exactly one 'lead' entry."""
    spawner = TeammateSpawner(
        __import__("pathlib").Path("/tmp/x"),
        loop_factory=lambda sid: None)
    spawner._teammates["lead"] = TeammateInfo(
        name="lead", role="boss", alive=True, started_at=time.time())
    project = _Project(spawner)
    from mini_cc.workflow.approvers import list_candidate_approvers
    candidates = list_candidate_approvers(project)
    lead_entries = [c for c in candidates if c["name"] == "lead"]
    assert len(lead_entries) == 1
