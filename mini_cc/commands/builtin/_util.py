"""Shared helpers for builtin command families (M3-1 split)."""
from __future__ import annotations

from ..registry import CommandContext


def _loop_of(ctx: CommandContext):
    sm = ctx.session_manager
    if sm is None:
        return None
    sess = sm._sessions.get((ctx.project_id, ctx.session_id))
    if sess is None:
        return None
    return getattr(sess, "loop", None)
