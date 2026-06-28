"""Round 2 audit Batch 7 tests — plan approval timeout (B5),
subagent MCP access (B10), workflow lock (B11), cron catch-up (B12)."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mini_cc.storage import FSStorage


# ── B5: plan approval timeout ──────────────────────────────────────────────

def test_plan_approval_timeout_is_configurable(tmp_path):
    """plan_approval_timeout default is 600s; can be overridden."""
    from mini_cc.teams import TeammateSpawner
    spawner = TeammateSpawner(
        workspace=tmp_path,
        loop_factory=lambda name: MagicMock(),
        project_id="p1",
        storage=FSStorage(tmp_path / "state"),
        plan_approval_timeout=42.0,
    )
    assert spawner.plan_approval_timeout == 42.0


def test_plan_approval_timeout_returns_message_on_expiry(tmp_path):
    """When the deadline elapses, _wait_for_plan_verdict returns a
    timeout message instead of blocking forever."""
    from mini_cc.teams import TeammateSpawner
    spawner = TeammateSpawner(
        workspace=tmp_path,
        loop_factory=lambda name: MagicMock(),
        project_id="p1",
        storage=FSStorage(tmp_path / "state"),
        idle_poll_interval=0.05,
        plan_approval_timeout=0.2,
    )
    info = MagicMock()
    info.name = "alice"
    verdict = spawner._wait_for_plan_verdict(info, "req-1")
    assert verdict is not None
    assert "timed out" in verdict.lower()


# ── B10: subagent MCP access ───────────────────────────────────────────────

def test_subagent_excludes_mcp_by_default():
    """spawn_subagent's signature accepts allow_mcp; default is False."""
    import inspect
    from mini_cc.core.subagent import spawn_subagent
    sig = inspect.signature(spawn_subagent)
    assert "allow_mcp" in sig.parameters
    assert sig.parameters["allow_mcp"].default is False


def test_task_tool_input_schema_includes_allow_mcp():
    from mini_cc.tools.subagent import TASK_TOOL
    assert "allow_mcp" in TASK_TOOL.input_schema["properties"]


# ── B11: workflow lock serializes concurrent run_all ───────────────────────

def test_workflow_run_all_lock_exists():
    """The workflow tool module exposes a per-wf-id lock helper."""
    from mini_cc.tools.workflow import _wf_lock
    lk1 = _wf_lock("wf_a")
    lk2 = _wf_lock("wf_a")
    lk3 = _wf_lock("wf_b")
    assert lk1 is lk2  # same id → same lock
    assert lk1 is not lk3


def test_workflow_lock_reentrant():
    """The same thread can re-acquire (no deadlock if a step re-enters)."""
    from mini_cc.tools.workflow import _wf_lock
    lk = _wf_lock("wf_reentrant_test")
    with lk:
        with lk:
            pass  # would deadlock if not reentrant


# ── B12: cron catch-up on restart ──────────────────────────────────────────

def test_cron_catches_up_missed_fires(tmp_path):
    """After a downtime spanning scheduled fires, the first tick on
    restart re-fires missed jobs once each."""
    from mini_cc.scheduler.cron import CronScheduler
    s = FSStorage(tmp_path / "state")
    sched = CronScheduler("p1", s)
    sched.schedule("*/1 * * * *", "check", recurring=True, durable=True)

    # Persist a last-tick from 5 minutes ago — implies 5 missed fires.
    past = datetime.now() - timedelta(minutes=5)
    s.write_tool_result("p1", "_cron_last_tick", past.isoformat())

    # Force load + tick at "now". The catch-up loop should enqueue.
    sched._ensure_loaded()
    fired = []
    # Drain manually: tick() enqueues into _fired_queue; consume().
    sched.tick(now=datetime.now())
    fired = sched.consume_fired()
    # We expect at least one catch-up fire (the most recent minute
    # matching */1 cron in the last 5 min window).
    assert any(job.prompt == "check" for job in fired), fired


def test_cron_no_catch_up_when_last_tick_missing(tmp_path):
    """First-ever run (no persisted tick) must not backfill the entire
    past — it'd fire everything matching the cron retroactively."""
    from mini_cc.scheduler.cron import CronScheduler
    s = FSStorage(tmp_path / "state")
    sched = CronScheduler("p1", s)
    sched.schedule("*/1 * * * *", "check", recurring=True, durable=True)
    sched._ensure_loaded()
    # No _cron_last_tick persisted → _last_tick_at is None.
    assert sched._last_tick_at is None
    sched.tick(now=datetime.now())
    fired = sched.consume_fired()
    # At most one fire (the current minute, if it matches).
    assert len(fired) <= 1


def test_cron_persists_last_tick(tmp_path):
    """tick() must persist the timestamp so the next restart can use it."""
    from mini_cc.scheduler.cron import CronScheduler
    s = FSStorage(tmp_path / "state")
    sched = CronScheduler("p1", s)
    sched.schedule("*/5 * * * *", "x", durable=True)
    sched.tick(now=datetime.now())
    raw = s.read_tool_result("p1", "_cron_last_tick")
    assert raw is not None
    datetime.fromisoformat(raw)  # parses
