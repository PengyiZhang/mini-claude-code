"""Candidate-approvers helper for checkpoint UI (debug.7.md Task 3b).

The checkpoint step's ``config.approvers`` is a free-form list of
strings. This module turns that into a structured list the UI can
render as a dropdown: active teammates first, then stopped teammates
(flagged), then the synthetic ``lead`` + ``human`` entries so the
user can always self-approve or type an arbitrary name.

The list is descriptive only — the approver string stored at resolve
time is whatever the caller sends. The HTTP layer (or UI) calls this
to populate its picker.
"""
from __future__ import annotations

from typing import Any, Protocol


class _HasTeams(Protocol):
    teams: Any


def list_candidate_approvers(project: Any) -> list[dict]:
    """Return a list of approver candidate dicts.

    Each dict has: ``name`` (str), ``type`` (``"teammate"`` |
    ``"lead"`` | ``"human"``), and for teammates ``alive`` (bool) +
    ``role`` (str).

    Order: alive teammates → stopped teammates → lead → human. A
    teammate literally named ``"lead"`` wins over the synthetic lead
    entry (the synthetic one is skipped) so the dedup is unambiguous.
    """
    out: list[dict] = []
    seen_names: set[str] = set()

    spawner = getattr(project, "teams", None)
    if spawner is not None:
        registry = getattr(spawner, "_teammates", {}) or {}
        # Sort: alive first, then by name for deterministic UI.
        infos = sorted(
            registry.values(),
            key=lambda i: (not getattr(i, "alive", False),
                           getattr(i, "name", "")))
        for info in infos:
            name = getattr(info, "name", "")
            if not name or name in seen_names:
                continue
            out.append({
                "name": name,
                "type": "teammate",
                "alive": bool(getattr(info, "alive", False)),
                "role": getattr(info, "role", "") or "",
            })
            seen_names.add(name)

    # Synthetic 'lead' entry — only if no teammate has that name.
    if "lead" not in seen_names:
        out.append({"name": "lead", "type": "lead", "alive": True,
                    "role": "main agent"})
        seen_names.add("lead")

    # 'human' placeholder — UI shows a free-form input when picked.
    out.append({"name": "", "type": "human", "alive": True,
                "role": "any human (type a name)"})
    return out


__all__ = ["list_candidate_approvers"]
