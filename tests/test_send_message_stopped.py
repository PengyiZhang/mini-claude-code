"""Task 1e (debug.7.md): warn when @mentioning a stopped teammate.

User symptom: second @alice (alice stopped) produced a confusing burst
of historical tool activities that vanished on refresh. Root cause:
events from the original spawn flow live in the SSE stream but never
hit the transcript; alice being stopped means no new events will ever
flow. Send into a stopped teammate's mailbox silently queues a
message that won't be processed.

Fix: send_message surfaces a clear warning when the recipient is a
known-but-stopped teammate, so the user knows to restart it (via
``/agents spawn`` or by re-running the original spawn tool) instead
of waiting indefinitely.
"""
from __future__ import annotations

import pytest

from mini_cc.storage import FSStorage
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext
from mini_cc.teams import TeammateSpawner


def _ctx(tmp_path, spawner=None):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    return ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], teams=spawner)


def test_send_message_warns_when_recipient_is_stopped(tmp_path):
    """Sending to a known-but-stopped teammate returns a warning
    mentioning the queued state — not the bare 'Sent to X' that
    implies successful delivery."""
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    # Inject a stopped teammate directly (no thread, no runner).
    from mini_cc.teams import TeammateInfo
    spawner._teammates["alice"] = TeammateInfo(
        name="alice", role="r", alive=False)
    ctx = _ctx(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["send_message"].handle(
        ctx, {"to": "alice", "content": "hi"})
    # Message IS delivered to the mailbox (will be drained on next spawn)
    # but the tool result must warn the user.
    assert "queued" in out.lower() or "stopped" in out.lower()
    # Mailbox retained the message.
    inbox = spawner.bus.peek_inbox("alice")
    assert any(m.get("content") == "hi" for m in inbox)


def test_send_message_silent_when_recipient_alive(tmp_path):
    """Alive teammate → no warning, just 'Sent to X'."""
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    from mini_cc.teams import TeammateInfo
    spawner._teammates["alice"] = TeammateInfo(
        name="alice", role="r", alive=True)
    ctx = _ctx(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["send_message"].handle(
        ctx, {"to": "alice", "content": "hi"})
    assert "queued" not in out.lower()
    assert "stopped" not in out.lower()
    assert "Sent to alice" in out


def test_send_message_silent_when_recipient_unknown(tmp_path):
    """Unknown recipient (no entry in spawner) — no warning. The message
    may be picked up by a future spawn; we don't second-guess the user."""
    spawner = TeammateSpawner(
        tmp_path / "ws", loop_factory=lambda sid: None)
    ctx = _ctx(tmp_path, spawner)
    tools = dispatch(builtin_tools())
    out = tools["send_message"].handle(
        ctx, {"to": "ghost", "content": "hi"})
    assert "queued" not in out.lower()
    assert "Sent to ghost" in out
