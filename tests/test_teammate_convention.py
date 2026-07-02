"""Task 4: built-in convention system prompt for spawned teammates.

Before this change, every teammate's first turn was seeded with an
`<identity>` block and the user-provided prompt — but the convention
(what the teammate is expected to do as a participant in this team's
protocol) lived only in the identity string, mixed with name/role.
That made it easy to drift: the identity told the teammate to send a
summary, but not when to call submit_plan, how to handle shutdown, or
how to acknowledge @mentions from the lead.

This module tests `convention_prompt()`, a single source of truth for
the team-protocol expectations every teammate inherits. The runner
prepends it to the user-provided prompt so prompt authors can focus on
task content instead of re-stating protocol rules in every spawn.
"""
from __future__ import annotations

import threading
import re

from mini_cc.teams import convention_prompt


def test_convention_prompt_is_non_empty_string():
    out = convention_prompt()
    assert isinstance(out, str)
    assert len(out) > 50  # not a stub


def test_convention_prompt_mentions_submit_plan():
    """The convention must tell the teammate to call submit_plan before
    doing non-trivial work — that's the whole point of the plan-approval
    gate. Without it the gate never triggers."""
    out = convention_prompt()
    assert "submit_plan" in out


def test_convention_prompt_mentions_send_message():
    """Teammates must report results back via send_message(to='lead', …).
    The convention has to spell out the recipient so the model doesn't
    send the summary to itself or drop it."""
    out = convention_prompt()
    assert "send_message" in out
    assert "lead" in out


def test_convention_prompt_mentions_shutdown():
    """A teammate must honor a shutdown_request from the lead. The
    convention must name the protocol so the model doesn't ignore the
    request as if it were ordinary inbox noise."""
    out = convention_prompt()
    assert "shutdown" in out


def test_convention_prompt_mentions_check_inbox_or_drain():
    """After completing a turn the teammate should expect the lead to
    inject inbox messages on the next turn — the convention must
    prepare it to act on those, not be surprised by them."""
    out = convention_prompt()
    lowered = out.lower()
    assert "inbox" in lowered or "drain" in lowered


def test_convention_prompt_stable_across_calls():
    """convention_prompt is module-level — same bytes every call so
    prompt caching across teammates works."""
    a = convention_prompt()
    b = convention_prompt()
    assert a == b


def test_runner_prepends_convention(tmp_path):
    """The spawner's _runner must inject the convention BEFORE the
    user-provided prompt so the model treats it as ground truth."""
    import threading
    from mini_cc.teams import TeammateSpawner, TeammateInfo, MessageBus

    captured: list[str] = []

    class FakeLoop:
        def run(self, user_input):
            captured.append(user_input)
            # Immediately stop the runner — return no events.
            return iter([])

    info = TeammateInfo(name="alice", role="tester", persistent=False,
                        prompt="do the thing")
    spawner = TeammateSpawner.__new__(TeammateSpawner)
    spawner.bus = MessageBus(tmp_path)
    spawner._lock = threading.Lock()
    spawner._teammates = {"alice": info}
    spawner._waiting_plan = {}
    spawner._waiting_shutdown = {}
    spawner._lead_events = []
    spawner._protocol = None
    spawner._loop_factory = lambda sid: FakeLoop()
    spawner._stopped_history = []
    spawner._keep_stopped = 3
    spawner._prune_stopped_locked = lambda: None
    spawner._send_shutdown_response = lambda *a, **kw: None
    spawner._idle_poll = lambda info, loop: ("timeout", None)
    spawner._wait_for_plan_verdict = lambda *a, **kw: None

    spawner._runner(info, "user-supplied prompt")
    assert captured, "runner did not invoke the loop"
    first = captured[0]
    conv_idx = first.find(convention_prompt()[:60])
    user_idx = first.find("user-supplied prompt")
    assert conv_idx != -1 and user_idx != -1
    assert conv_idx < user_idx, (
        "convention must come before user prompt in assembled input; "
        f"conv@{conv_idx} user@{user_idx}"
    )


# ── Bug 3: teammate→lead events must persist into the lead session ──


def _make_spawner_for_lead_events(tmp_path):
    """Build a minimal TeammateSpawner wired with a fake storage so we
    can verify teammate→lead events land in the session transcript even
    when no /send SSE stream is running to drain them."""
    from collections import deque
    from mini_cc.teams import MessageBus, TeammateSpawner
    spawner = TeammateSpawner.__new__(TeammateSpawner)
    spawner.bus = MessageBus(tmp_path)
    spawner.bus.set_lead_hook(spawner._emit_to_lead)
    spawner._lock = threading.Lock()
    spawner._lead_events = deque()
    spawner.project_id = "proj-L"
    persisted = []
    class _FakeStorage:
        def append_session_event(self, pid, sid, ev):
            persisted.append((pid, sid, ev))
    spawner.storage = _FakeStorage()
    spawner._lead_session_id = None
    return spawner, persisted


def test_emit_to_lead_persists_when_session_bound(tmp_path):
    """When set_lead_session has been called, every teammate→lead
    message persists to that session's transcript so a page refresh
    replays it. Pre-fix the event queued in _lead_events was only
    drained inside /send — a refresh while lead was idle lost the
    message entirely."""
    spawner, persisted = _make_spawner_for_lead_events(tmp_path)
    spawner.set_lead_session("sess-lead")
    spawner.bus.send("alice", "lead", "hi lead", "result")
    assert len(persisted) == 1
    pid, sid, ev = persisted[0]
    assert pid == "proj-L"
    assert sid == "sess-lead"
    assert ev["type"] == "teammate_message"
    assert ev["from"] == "alice"
    assert ev["content"] == "hi lead"


def test_emit_to_lead_skips_persist_when_no_session(tmp_path):
    """Without a bound lead session, persistence is skipped — but the
    event still queues in _lead_events so a live /send can drain it."""
    spawner, persisted = _make_spawner_for_lead_events(tmp_path)
    spawner.bus.send("alice", "lead", "hi lead")
    assert persisted == []
    drained = spawner.drain_lead_events()
    assert len(drained) == 1


def test_set_lead_session_round_trip(tmp_path):
    """Basic set/clear contract: passing None clears it."""
    spawner, _ = _make_spawner_for_lead_events(tmp_path)
    assert spawner._lead_session_id is None
    spawner.set_lead_session("s1")
    assert spawner._lead_session_id == "s1"
    spawner.set_lead_session(None)
    assert spawner._lead_session_id is None
