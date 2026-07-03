"""Lead-side mailbox injection — late teammate replies must reach the
lead's LLM context on the next turn.

Scenario (bug): user types to lead → lead's loop runs → lead calls
check_inbox → empty (alice hasn't replied yet) → lead's turn ends.
THEN alice (running in her own thread) finishes and sends a message
to lead. The message lands in lead's mailbox (drainable by check_inbox)
and in _lead_events (drained by /send SSE for the UI), but it never
enters loop.messages — so on the next user input, lead's LLM has no
idea alice replied and acts as if the conversation stalled.

Fix: drain lead's mailbox at the start of every loop iteration and
inject as a user-role note, mirroring _inject_background_notifications.
The teammate subsystem already does the same thing via _idle_poll for
its own agents; this closes the asymmetry for the lead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Thread

import pytest

from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.teams import MessageBus


@dataclass
class _Block:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict | None = None
    id: str | None = None


class _MockResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _MockClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

        class _Stream:
            def __init__(s, r): s._r = r

            def __enter__(s): return s
            def __exit__(s, *e): return False
            def __iter__(s): return iter(())
            def get_final_message(s): return s._r
            def close(s): pass

        class _M:
            def stream(s, **kw):
                self.calls.append(kw)
                if not self.script:
                    raise RuntimeError("script exhausted")
                return _Stream(self.script.pop(0))

        self._m = _M()

    @property
    def messages(self):
        return self._m


@dataclass
class _SpawnerStub:
    bus: MessageBus


def _build_loop(tmp_path, script):
    sandbox = SubprocessSandbox("proj-x", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    bus = MessageBus(tmp_path / "ws")
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-x", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        teams=_SpawnerStub(bus=bus),
    )
    return AgentLoop(ref, "sess-lead"), bus


def _run_in_thread(loop, user_input):
    events: list[dict] = []
    error: list = []

    def _runner():
        try:
            for ev in loop.run(user_input):
                events.append(ev)
        except Exception as e:
            error.append(e)

    t = Thread(target=_runner)
    t.start()
    t.join(timeout=15)
    assert not t.is_alive(), "loop thread did not finish"
    if error:
        raise error[0]
    return events


def test_lead_mailbox_drained_into_loop_messages(tmp_path):
    """A late teammate→lead reply sitting in lead's mailbox must be
    injected into loop.messages as a user-role note before the LLM
    thinks, so the model sees the reply in context."""
    script = [
        _MockResponse([_Block(type="text", text="got your reply")]),
    ]
    loop, bus = _build_loop(tmp_path, script)
    # Simulate alice's late reply landing before /send runs.
    bus.send("alice", "lead", "done: result is 42", msg_type="result")

    _run_in_thread(loop, "what did alice say?")

    # The mailbox drain should have injected alice's message as a
    # user-role entry tagged with <teammate_messages>.
    injected = [
        m for m in loop.messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "<teammate_messages>" in m["content"]
    ]
    assert injected, (
        "late teammate reply was not injected into loop.messages; "
        f"messages were: {loop.messages}"
    )
    assert "result is 42" in injected[0]["content"]
    # The mailbox is drained — subsequent check_inbox returns empty
    # (consistent with teammate _idle_poll semantics).
    assert bus.peek_inbox("lead") == []


def test_no_injection_when_mailbox_empty(tmp_path):
    """An empty mailbox must not inject any noise — no spurious
    <teammate_messages> note when there's nothing to say."""
    script = [_MockResponse([_Block(type="text", text="hi")])]
    loop, _ = _build_loop(tmp_path, script)
    _run_in_thread(loop, "hello")
    injected = [
        m for m in loop.messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and "<teammate_messages>" in m["content"]
    ]
    assert injected == []


def test_teammate_session_skips_lead_mailbox_drain(tmp_path):
    """A teammate's own loop must NOT drain lead's mailbox — only the
    lead session does that. Without this guard, a teammate would steal
    lead's mail whenever it happened to be running."""
    script = [_MockResponse([_Block(type="text", text="ok")])]
    loop, bus = _build_loop(tmp_path, script)
    loop.session_id = "teammate-alice"  # simulate teammate session
    bus.send("alice", "lead", "should not be touched", msg_type="result")

    _run_in_thread(loop, "working...")
    # Mailbox must be untouched because the loop is a teammate session.
    assert bus.peek_inbox("lead"), (
        "teammate session drained lead's mailbox — guard missing"
    )


def test_no_injection_when_teams_none(tmp_path):
    """A project without the teams subsystem must not crash on the
    injection path. Regression for projects that predate teams."""
    script = [_MockResponse([_Block(type="text", text="ok")])]
    sandbox = SubprocessSandbox("proj-x", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    from mini_cc.core.llm import AnthropicProvider
    client = _MockClient(script)
    ref = ProjectRef(
        project_id="proj-x", project_root=str(tmp_path / "ws"),
        sandbox=sandbox, storage=storage,
        client_factory=lambda: AnthropicProvider(lambda: client),
        teams=None,
    )
    loop = AgentLoop(ref, "sess-lead")
    # Must not raise.
    _run_in_thread(loop, "hi")
