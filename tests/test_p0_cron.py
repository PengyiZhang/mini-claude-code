"""CronScheduler + cron tools tests."""
from __future__ import annotations

from datetime import datetime

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.scheduler import CronScheduler, cron_matches, validate_cron
from mini_cc.storage import FSStorage
from mini_cc.tools import builtin_tools, dispatch
from mini_cc.tools.base import ToolContext


# ── Pure-function cron parsing ────────────────────────────────────────────

def test_validate_cron_accepts_standard():
    assert validate_cron("0 9 * * *") is None
    assert validate_cron("*/5 * * * *") is None
    assert validate_cron("0 9 1-7 * 1-5") is None
    assert validate_cron("0,30 * * * *") is None


def test_validate_cron_rejects_garbage():
    assert validate_cron("not a cron") is not None
    assert validate_cron("* * * *") is not None  # only 4 fields
    assert validate_cron("60 * * * *") is not None  # minute out of range
    assert validate_cron("*/0 * * * *") is not None


def test_cron_matches_every_minute():
    dt = datetime(2026, 6, 20, 14, 30, 0)
    assert cron_matches("* * * * *", dt)


def test_cron_matches_specific_time():
    dt = datetime(2026, 6, 20, 9, 5, 0)
    assert cron_matches("5 9 * * *", dt)
    assert not cron_matches("6 9 * * *", dt)


def test_cron_step_pattern():
    # Every 15 minutes should match minute 0, 15, 30, 45
    for m in [0, 15, 30, 45]:
        assert cron_matches("*/15 * * * *", datetime(2026, 1, 1, 12, m))
    assert not cron_matches("*/15 * * * *", datetime(2026, 1, 1, 12, 7))


# ── CronScheduler ─────────────────────────────────────────────────────────

def _scheduler(tmp_path):
    storage = FSStorage(tmp_path / "state")
    return CronScheduler("proj-a", storage), storage


def test_schedule_then_list(tmp_path):
    s, _ = _scheduler(tmp_path)
    job, err = s.schedule("0 9 * * *", "check")
    assert err is None
    assert job.job_id.startswith("cron_")
    jobs = s.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].prompt == "check"


def test_schedule_invalid_returns_error(tmp_path):
    s, _ = _scheduler(tmp_path)
    job, err = s.schedule("not-a-cron", "x")
    assert job is None
    assert err is not None
    assert s.list_jobs() == []


def test_cancel(tmp_path):
    s, _ = _scheduler(tmp_path)
    job, _ = s.schedule("0 9 * * *", "check")
    assert s.cancel(job.job_id) == f"Cancelled {job.job_id}"
    assert s.list_jobs() == []
    assert "not found" in s.cancel("missing")


def test_durable_persisted_to_storage(tmp_path):
    s, storage = _scheduler(tmp_path)
    s.schedule("0 9 * * *", "durable", durable=True)
    s.schedule("0 10 * * *", "session", durable=False)
    persisted = storage.load_cron("proj-a")
    assert len(persisted) == 1  # only durable saved
    assert persisted[0].prompt == "durable"


def test_durable_jobs_loaded_on_new_instance(tmp_path):
    s1, _ = _scheduler(tmp_path)
    s1.schedule("0 9 * * *", "remember me", durable=True)
    # New scheduler reading from the same storage should pick it up
    storage2 = FSStorage(s1.storage.root)
    s2 = CronScheduler("proj-a", storage2)
    jobs = s2.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].prompt == "remember me"


def test_tick_enqueues_matching_jobs(tmp_path):
    s, _ = _scheduler(tmp_path)
    s.schedule("* * * * *", "every minute", durable=False)
    now = datetime(2026, 6, 20, 14, 30, 0)
    s.tick(now=now)
    fired = s.consume_fired()
    assert len(fired) == 1
    assert fired[0].prompt == "every minute"
    # Same minute marker: should not re-fire
    s.tick(now=now)
    assert s.consume_fired() == []


def test_tick_one_shot_removes_after_fire(tmp_path):
    s, _ = _scheduler(tmp_path)
    s.schedule("* * * * *", "once", recurring=False, durable=False)
    now = datetime(2026, 6, 20, 14, 30, 0)
    s.tick(now=now)
    assert s.consume_fired()
    assert s.list_jobs() == []  # one-shot removed


# ── Cron tools ────────────────────────────────────────────────────────────

def _ctx(tmp_path, scheduler):
    sandbox = SubprocessSandbox("proj-a", tmp_path / "ws")
    storage = scheduler.storage
    return ToolContext(project_id="proj-a", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], scheduler=scheduler)


def test_schedule_tool_creates_job(tmp_path):
    s, _ = _scheduler(tmp_path)
    ctx = _ctx(tmp_path, s)
    tools = dispatch(builtin_tools())
    out = tools["schedule_cron"].handle(ctx, {"cron": "0 9 * * *", "prompt": "x"})
    assert "Scheduled cron_" in out
    assert len(s.list_jobs()) == 1


def test_schedule_tool_rejects_invalid(tmp_path):
    s, _ = _scheduler(tmp_path)
    ctx = _ctx(tmp_path, s)
    tools = dispatch(builtin_tools())
    out = tools["schedule_cron"].handle(ctx, {"cron": "garbage", "prompt": "x"})
    assert "Error" in out
    assert s.list_jobs() == []


def test_list_tool_empty(tmp_path):
    s, _ = _scheduler(tmp_path)
    ctx = _ctx(tmp_path, s)
    tools = dispatch(builtin_tools())
    assert tools["list_crons"].handle(ctx, {}) == "No cron jobs."


def test_cancel_tool(tmp_path):
    s, _ = _scheduler(tmp_path)
    job, _ = s.schedule("0 9 * * *", "x")
    ctx = _ctx(tmp_path, s)
    tools = dispatch(builtin_tools())
    out = tools["cancel_cron"].handle(ctx, {"job_id": job.job_id})
    assert "Cancelled" in out
    assert s.list_jobs() == []


def test_cron_tools_without_scheduler(tmp_path):
    sandbox = SubprocessSandbox("p", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ctx = ToolContext(project_id="p", session_id="s", sandbox=sandbox,
                      storage=storage, todos=[], scheduler=None)
    tools = dispatch(builtin_tools())
    assert "not configured" in tools["schedule_cron"].handle(
        ctx, {"cron": "0 9 * * *", "prompt": "x"}).lower()
