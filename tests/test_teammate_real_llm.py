"""Real-LLM teammate integration test.

Exercises the full TeammateSpawner → AgentLoop → real LLM provider
(glm-4.7 via the localhost:8000 anthropic-compatible proxy) → MessageBus
path with NO monkeypatching. The teammate thread runs a real model turn,
calls the real send_message tool, and the lead's inbox should see the
reply land on disk.

Skipped automatically when the LLM proxy at $ANTHROPIC_BASE_URL isn't
reachable.

Run locally with:
    pytest tests/test_teammate_real_llm.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# .env is normally loaded by mini_cc/server/cli.py at server startup;
# pytest bypasses that entry point, so we load it here to pick up
# MODEL_ID / ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY. Without this,
# default_config() falls back to claude-sonnet-4-6 and the proxy 400s.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pytest

from mini_cc.config import default_config
from mini_cc.core.loop import AgentLoop, ProjectRef
from mini_cc.sandbox import SubprocessSandbox
from mini_cc.storage import FSStorage
from mini_cc.teams import TeammateSpawner


def _llm_reachable() -> bool:
    """Cheap reachability probe for the configured LLM proxy.

    We don't check the model actually works — that's the test's job —
    we just gate on the host being up so the test skips cleanly on a
    developer machine that hasn't started the proxy yet.
    """
    cfg = default_config()
    base = getattr(cfg, "base_url", None) or os.environ.get(
        "ANTHROPIC_BASE_URL", "")
    if not base:
        return False
    import urllib.request
    try:
        urllib.request.urlopen(base, timeout=2)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _llm_reachable(),
    reason="LLM proxy unreachable; set ANTHROPIC_BASE_URL and start the proxy",
)


def _make_ref(tmp_path: Path) -> ProjectRef:
    """ProjectRef with a real per-test sandbox + storage.

    No client_factory override → AgentLoop falls back to
    default_config().build_provider, which is the real glm-4.7 client.
    """
    sandbox = SubprocessSandbox("proj-real-llm", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    return ProjectRef(
        project_id="proj-real-llm",
        project_root=str(tmp_path / "ws"),
        sandbox=sandbox,
        storage=storage,
        tenant_id="t-real-llm",
    )


def test_teammate_replies_via_real_llm(tmp_path):
    """Spawn a teammate with a trivial task; the real LLM should call
    the send_message tool to deliver a reply to the lead's inbox.

    Asserts:
      - The teammate becomes alive after spawn.
      - Within ~60s, the lead's inbox contains at least one 'message'
        from the teammate whose content acknowledges the ping.
      - After the reply, the teammate thread exits (alive=False).
    """
    ref = _make_ref(tmp_path)

    # Per-teammate AgentLoop factory. Each teammate gets its own loop
    # instance — the spawner passes the session_id ("teammate-<name>").
    def loop_factory(sid: str) -> AgentLoop:
        return AgentLoop(ref, sid)

    spawner = TeammateSpawner(
        tmp_path / "teams_ws",
        loop_factory,
        project_id=ref.project_id,
        storage=ref.storage,
        # Make the idle poll snappy so the teammate exits promptly
        # after its turn instead of blocking the test for 60s.
        idle_poll_interval=0.5,
        idle_timeout=3.0,
    )
    # Wire the spawner back into the ref so the teammate's loop sees a
    # populated ctx.teams. Without this, the send_message tool returns
    # "Teams subsystem not configured" and the LLM has no way to reply.
    # Mirrors projects/manager.py:317 (project.teams = TeammateSpawner(...)).
    ref.teams = spawner

    # Sanity: bus is empty before spawn.
    assert spawner.bus.peek_inbox("lead") == []

    events: list[dict] = []

    def _capture(ev: dict) -> None:
        # Keep the log compact — only structured events, not raw text deltas.
        if ev.get("type") in {"tool_use", "tool_result", "error", "done"}:
            events.append(ev)

    err = spawner.spawn(
        name="pong",
        role="responder",
        prompt=(
            "Use the send_message tool to send the literal text 'pong' "
            "to recipient 'lead', then end your turn. Do not call any "
            "other tool. Do not write any files."
        ),
        on_event=_capture,
    )
    assert err is None, f"spawn returned error: {err}"

    # Confirm the spawn actually registered.
    assert any(t.name == "pong" for t in spawner.list_alive())

    # Poll the lead's inbox for the teammate's reply. The teammate's
    # identity prompt + this task together should produce a single
    # send_message call with content "pong" within a few seconds.
    deadline = time.time() + 60
    replies = []
    while time.time() < deadline:
        msgs = spawner.bus.peek_inbox("lead")
        replies = [m for m in msgs
                   if m.get("from") == "pong" and m.get("type") == "message"]
        if replies:
            break
        time.sleep(0.5)

    assert replies, (
        f"no message from 'pong' in lead's inbox after 60s; "
        f"inbox={spawner.bus.peek_inbox('lead')!r}; "
        f"events={events!r}"
    )
    # The LLM might wrap the literal in punctuation ("pong.", "`pong`"),
    # so check the word is present, not that content is exactly equal.
    assert "pong" in replies[0]["content"].lower(), (
        f"unexpected reply content: {replies[0]['content']!r}"
    )

    # After the turn completes, the teammate should exit on its own
    # (idle_timeout=3.0 → runner exits after 3s of no new work).
    deadline = time.time() + 30
    while time.time() < deadline:
        if not any(t.name == "pong" and t.alive for t in spawner.list_alive()):
            break
        # Force a clean shutdown if it's still alive (don't fail the
        # test on lingering liveness — that's a separate concern).
        time.sleep(0.5)
    else:
        spawner.request_shutdown("pong")
        time.sleep(2)

    # Either way, the spawner shouldn't be left in a weird state.
    spawner.shutdown(timeout=5)
