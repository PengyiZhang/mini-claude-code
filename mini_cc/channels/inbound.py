"""Inbound turn injection — fire-and-forget background thread that runs
``SessionManager.send`` against the bound session.

Extracted from ``mini_cc/server/routes/channels.py`` so both the HTTP
webhook path and the WS receiver path share one implementation. Errors
are swallowed — the caller (HTTP webhook or WS event handler) has
already moved on; surfacing a failure here would orphan the user's
message. The agent loop's own error handling emits ``{"type":"error"}``
events that flow back through the channel via the dispatcher.

Events are also persisted to ``events.jsonl`` (best-effort) so the
``/events`` SSE tail stream surfaces inbound-channel-driven turns in the
live UI. Mirrors the persistence in ``routes/sessions.py`` /send path.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .feishu_common import TokenCache, download_image

if TYPE_CHECKING:
    from ..projects import Project
    from ..session import SessionManager

log = logging.getLogger("mini_cc.channels.inbound")


# In-flight channel-inbound workers (M3-5). Daemon threads by design —
# they must not block process exit — but lifespan shutdown drains them
# so a turn already talking to the LLM gets its sink writes flushed
# instead of being axed mid-flight. Shared by the HTTP webhook path and
# the WS receiver path since both enqueue through this module.
_WORKERS: set = set()
_WORKERS_LOCK = threading.Lock()


def drain_inbound_workers(timeout: float = 10.0) -> None:
    """Join in-flight channel-inbound workers (bounded by ``timeout``).

    Workers that overrun the timeout stay daemon-dropped, as before."""
    with _WORKERS_LOCK:
        workers = [w for w in _WORKERS if w.is_alive()]
    import time as _time
    deadline = _time.monotonic() + timeout
    for w in workers:
        remaining = max(0.0, deadline - _time.monotonic())
        w.join(timeout=remaining)
    with _WORKERS_LOCK:
        for w in list(_WORKERS):
            if not w.is_alive():
                _WORKERS.discard(w)


def enqueue_inbound_turn(sm: "SessionManager",
                         project: "Project",
                         session_id: str | None,
                         user_input: str,
                         metadata: dict) -> threading.Thread:
    """Spawn a daemon thread that drives ``sm.send`` for one turn.

    The session is resolved lazily: when ``session_id`` is None we fall
    back to the project's most-recent lead session (or create one named
    ``chan`` if none exists) so a brand-new channel binding can be the
    user's first message.

    When ``metadata["kind"] == "image"`` the worker constructs a
    ``TokenCache`` from ``metadata["_binding_config"]`` (set by the
    channel supervisor before enqueue), downloads each Feishu
    ``image_key`` via ``download_image``, persists the bytes through
    ``project.assets.put``, and builds a content-blocks list
    (``[{type:text,...?}, {type:image, asset_id}, ...]``) that is handed
    to ``sm.send``. Download/asset-put happens INSIDE the worker so the
    inbound webhook returns fast — Feishu's webhook timeout is tight
    (~3s) and the LLM stream + image downloads can take much longer.

    On per-image failure the worker substitutes a text placeholder block
    rather than crashing — the lead still sees a turn surface with
    "[image: download failed: ...]" so the operator notices.
    """
    pid = project.project_id

    def _worker():
        try:
            sid = session_id or _resolve_default_session(project, sm)
            if sid is None:
                log.warning(
                    "channel inbound turn dropped: no session to route "
                    "into (project=%s)", pid)
                return
            effective_input = _maybe_build_image_blocks(user_input,
                                                        metadata, project)
            for ev in sm.send(pid, sid, effective_input):
                # Persist every event so the /events tail stream picks
                # up inbound-channel turns in the live UI. Best-effort —
                # write failure doesn't break the turn (the dispatcher
                # already pushed the event to channels via on_event).
                try:
                    project.storage.append_session_event(pid, sid, ev)
                except Exception:
                    log.exception(
                        "could not persist inbound event for %s/%s",
                        pid, sid)
        except Exception:
            log.warning(
                "channel inbound turn failed: project=%s session=%s "
                "input=%r", pid, session_id, user_input[:120],
                exc_info=True)
        finally:
            with _WORKERS_LOCK:
                _WORKERS.discard(threading.current_thread())

    t = threading.Thread(target=_worker, daemon=True,
                         name=f"channel-inbound:{pid}")
    with _WORKERS_LOCK:
        _WORKERS.add(t)
    t.start()
    return t


def _maybe_build_image_blocks(user_input, metadata: dict, project):
    """If ``metadata`` describes an image turn, return a list[dict] of
    Anthropic-style content blocks (text + image refs) downloaded +
    persisted via ``project.assets``. Otherwise return ``user_input``
    unchanged (legacy string text turn).

    Called from inside ``_worker`` so download/put latency is owned by
    the background thread, not the inbound webhook caller.
    """
    if metadata.get("kind") != "image":
        return user_input

    binding_config = metadata.get("_binding_config") or {}
    cache = TokenCache(
        binding_config.get("app_id", ""),
        binding_config.get("app_secret", ""),
    )
    source = metadata.get("source", "feishu")
    blocks: list[dict] = []
    if user_input:
        blocks.append({"type": "text", "text": user_input})
    msg_id = metadata.get("message_id", "")
    for image_key in metadata.get("image_keys", []):
        try:
            data, media_type = download_image(msg_id, image_key, cache)
            aid = project.assets.put(
                data, media_type=media_type,
                src=f"{source}:{image_key}")
            blocks.append({"type": "image", "asset_id": aid})
        except Exception as e:
            log.warning("image download failed for %s/%s: %s",
                        source, image_key, e)
            blocks.append({"type": "text",
                           "text": f"[image: download failed: {e}]"})
    return blocks


def _resolve_default_session(project: "Project",
                             sm: "SessionManager") -> str | None:
    """Pick or create the project's default lead session for channel
    inbound. Heuristic: reuse the most-recently-warmed non-teammate
    session; otherwise create a new one named ``chan`` so it's easy to
    spot in the UI as the channel-bound session."""
    metas = sm.list(project.project_id)
    lead_metas = [m for m in metas
                  if not m.session_id.startswith("teammate-")]
    if lead_metas:
        lead_metas.sort(key=lambda m: getattr(m, "created_at", ""),
                        reverse=True)
        return lead_metas[0].session_id
    sess = sm.start_session(project.project_id, "chan")
    return sess.session_id


__all__ = ["enqueue_inbound_turn"]
