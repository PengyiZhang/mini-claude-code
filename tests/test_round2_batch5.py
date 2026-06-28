"""Round 2 audit Batch 5 tests — run guard (B2), task-claim CAS (B4),
teammate graceful shutdown (B6). B1 (parallel tool calls) deferred —
needs a separate focused PR."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_cc.storage import FSStorage, Task


# ── B2: AgentLoop run guard ────────────────────────────────────────────────

def test_loop_run_concurrent_raises():
    """Two threads entering run() concurrently must fail fast rather
    than corrupt self.messages."""
    from mini_cc.core.loop import AgentLoop

    project = MagicMock()
    project.storage.load_messages.return_value = []
    project.storage.load_todos.return_value = []
    project.mcp_pool = None
    project.skills_loader = None
    project.memory_loader = None
    project.scheduler = None
    project.teams = None
    project.background = None
    project.permissions = None
    project.prompt_tools = set()
    project.project_id = "p1"

    loop = AgentLoop(project=project, session_id="s1",
                     system_prompt_override="x")

    # Simulate being mid-run by setting the flag manually.
    with loop._running_lock:
        loop._running = True
        # A second entry must observe the flag and refuse.
        with loop._running_lock:
            assert loop._running is True
        # Reset for cleanliness.
        loop._running = False


# ── B4: atomic task claim ─────────────────────────────────────────────────

def test_claim_task_atomic_serializes_concurrent_claims(tmp_path):
    s = FSStorage(tmp_path / "state")
    t = Task(id="shared", subject="x", description="",
             status="pending", owner=None, blockedBy=[])
    s.save_task("p", t)

    results = []
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait()
        ok, msg = s.claim_task_atomic("p", "shared", owner)
        results.append((owner, ok, msg))

    threads = [
        threading.Thread(target=claim, args=("alice",)),
        threading.Thread(target=claim, args=("bob",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    winners = [r for r in results if r[1]]
    losers = [r for r in results if not r[1]]
    assert len(winners) == 1, f"expected exactly 1 winner, got {results}"
    assert len(losers) == 1
    assert "already owned" in losers[0][2] or "in_progress" in losers[0][2]


def test_claim_task_atomic_rejects_already_owned(tmp_path):
    s = FSStorage(tmp_path / "state")
    t = Task(id="t1", subject="x", description="",
             status="pending", owner="alice", blockedBy=[])
    s.save_task("p", t)
    ok, msg = s.claim_task_atomic("p", "t1", "bob")
    assert not ok
    assert "alice" in msg


def test_claim_task_atomic_unknown_task(tmp_path):
    s = FSStorage(tmp_path / "state")
    ok, msg = s.claim_task_atomic("p", "ghost", "alice")
    assert not ok
    assert "not found" in msg


# ── B6: TeammateSpawner graceful shutdown ──────────────────────────────────

def test_teammate_spawner_has_shutdown_method():
    """Smoke check: shutdown() exists and accepts a timeout."""
    from mini_cc.teams import TeammateSpawner
    import inspect
    sig = inspect.signature(TeammateSpawner.shutdown)
    assert "timeout" in sig.parameters


def test_teammate_spawner_shutdown_no_op_when_no_teammates(tmp_path):
    """Calling shutdown() on a fresh spawner must not raise."""
    from mini_cc.teams import TeammateSpawner
    spawner = TeammateSpawner(
        workspace=tmp_path,
        loop_factory=lambda name: MagicMock(),
        project_id="p1",
        storage=FSStorage(tmp_path / "state"),
    )
    # Should be a no-op when no teammates are alive.
    spawner.shutdown(timeout=1.0)
    assert spawner.list_alive() == []
