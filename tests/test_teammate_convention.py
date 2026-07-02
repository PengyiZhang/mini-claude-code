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
