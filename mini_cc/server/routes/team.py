"""Phase I.C.1 — Team activity aggregation endpoint.

``GET /tenants/{tid}/projects/{pid}/team/activity`` merges events.jsonl
records across every session in the project into a single timeline,
filterable by ``ts > since`` and optional ``teammate``. Drives the
TeammatesPanel upgrade (I.C.3) and the future Team tab (I.C.4).

Design notes (see docs/plans/2026-07-03-phaseI-team-orchestration-design.md,
Task I.C.1):

* ``ts`` is stamped onto the JSONL record by ``FSStorage.append_session_event``.
  ``iter_session_events_with_ts`` is the only reader that consumes it.
* Teammate session ids follow the convention ``teammate-<name>`` (see
  ``mini_cc/teams/__init__.py:TeammateSpawner._runner``). The lead
  session has no such prefix. ``?teammate=alice`` filters to
  ``teammate-alice`` only.
* The endpoint is read-only — no caching, no webhook firing. The UI
  polls every 4s (Task I.C.3).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Query

from ..deps import get_pm, require_scope, validate_id
from ..errors import NotFound

router = APIRouter(
    prefix="/tenants/{tid}/projects/{pid}/team",
    tags=["team"],
)


def _check_project_tenant(pid: str, tid: str, pm) -> None:
    """Ensure the project exists and belongs to this tenant. Raises 404
    for missing or cross-tenant access. Mirrors the helper in
    sessions.py — kept local so this router stays self-contained."""
    try:
        p = pm.get(pid)
    except KeyError as e:
        raise NotFound(str(e) or f"project {pid} not found")
    if p.meta.tenant_id != tid:
        raise NotFound(f"project {pid} not found")


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp defensively. Returns None on
    failure so the caller can skip the record rather than 500."""
    if not ts:
        return None
    try:
        # ``fromisoformat`` accepts the ``Z`` suffix only on 3.11+; the
        # storage layer writes ``Z`` (see _iso_now in fs.py), so handle
        # both spellings.
        norm = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(norm)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/activity")
def get_team_activity(
    pid: str = Path(...),
    tid: str = Depends(require_scope("sessions:read")),
    since: str | None = Query(
        None, description="ISO timestamp; events with ts > since returned"),
    limit: int = Query(200, ge=1, le=1000),
    teammate: str | None = Query(
        None, description="Filter to a single teammate's session"),
    pm=Depends(get_pm),
) -> dict:
    """Aggregate events across the project's sessions, filtered by ts
    and optional teammate. Returns a unified timeline for the UI's
    TeammatesPanel and future Team tab.

    Response shape: ``{"events": [{session_id, ts, ...payload}],
    "has_more": bool}``. Events are sorted by ts ascending; ties keep
    their original insertion order (Python's sort is stable).
    """
    validate_id(pid)
    _check_project_tenant(pid, tid, pm)
    project = pm.get(pid)

    # Resolve the set of session ids we'll scan. If a teammate filter
    # is supplied, narrow to that single session id; otherwise scan
    # every session that has an on-disk events.jsonl log. We use
    # list_session_event_logs (not list_sessions) so sessions that have
    # emitted events but not yet flushed a message-index entry are
    # still surfaced — the message index is a write-side cache and
    # shouldn't gate read-only observability.
    if teammate is not None:
        # Defensive: teammate names are validated downstream by the
        # spawner, but we still gate the prefix so a malicious value
        # can't be used to enumerate arbitrary session ids.
        target_sid = f"teammate-{teammate}"
        candidate_sids = [target_sid]
    else:
        if hasattr(project.storage, "list_session_event_logs"):
            candidate_sids = project.storage.list_session_event_logs(pid)
        else:
            # Fallback for storage backends without the discovery helper.
            candidate_sids = [m.session_id
                              for m in project.storage.list_sessions(pid)]

    since_dt = _parse_iso(since) if since else None

    collected: list[dict] = []
    for sid in candidate_sids:
        try:
            for rec in project.storage.iter_session_events_with_ts(pid, sid):
                if since_dt is not None:
                    rec_dt = _parse_iso(rec.get("ts", ""))
                    if rec_dt is None or rec_dt <= since_dt:
                        continue
                payload = rec.get("payload") or {}
                # Spread payload fields alongside session_id/ts so the
                # UI can read type/text/etc. without an extra hop.
                # Some payloads carry their own ``ts`` (e.g.
                # ``teammate_message`` stamps a unix-float for the
                # bus), which would clobber the top-level ISO string
                # and break the timeline sort below. Strip ``ts`` and
                # ``session_id`` from the payload before spread so the
                # top-level values win.
                if isinstance(payload, dict):
                    stripped = {k: v for k, v in payload.items()
                                if k not in ("ts", "session_id")}
                    out = {"session_id": rec["session_id"],
                           "ts": rec["ts"], **stripped}
                else:
                    out = {"session_id": rec["session_id"],
                           "ts": rec["ts"], "payload": payload}
                collected.append(out)
        except Exception:
            # A corrupted/unreadable session log must not break the
            # whole timeline — skip it.
            continue

    # Sort by ts ascending; stable sort preserves per-session insertion
    # order for same-ts ties (e.g. events stamped within the same
    # millisecond).
    collected.sort(key=lambda e: e["ts"])

    has_more = len(collected) > limit
    truncated = collected[:limit]
    return {"events": truncated, "has_more": has_more}
