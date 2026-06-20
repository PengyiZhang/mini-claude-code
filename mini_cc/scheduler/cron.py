"""Per-project cron scheduler.

Ports s20 lines 1330-1500 from s20_comprehensive/code.py with these changes:
- No module-global scheduled_jobs / cron_queue / cron_lock — instance state
- Persisted via Storage (which is already keyed by project_id)
- No background thread: tick() is called by the AgentLoop at the top of each
  iteration. Out-of-session firing (when no agent is running) needs a shared
  ticker thread — TODO for a later port.

Cron semantics are 5-field standard (minute hour dom month dow), with the
standard "*" / "*/N" / "N-M" / "N,M" / literal syntax. DOW is 0-6 (Sun=0).
"""
from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Optional

from ..storage import CronJob, Storage


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(p.strip(), value) for p in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    # POSIX: DOW 0-6 with Sunday=0. Python weekday(): Mon=0..Sun=6.
    dow_val = (dt.weekday() + 1) % 7
    if not (_cron_field_matches(minute, dt.minute)
            and _cron_field_matches(hour, dt.hour)
            and _cron_field_matches(month, dt.month)):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return _cron_field_matches(dow, dow_val)
    if dow == "*":
        return _cron_field_matches(dom, dt.day)
    return _cron_field_matches(dom, dt.day) or _cron_field_matches(dow, dow_val)


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


class CronScheduler:
    """Per-project cron scheduler. Stateless across restarts except for
    durable jobs (persisted via Storage)."""

    def __init__(self, project_id: str, storage: Storage):
        self.project_id = project_id
        self.storage = storage
        self._lock = threading.Lock()
        self._jobs: dict[str, CronJob] = {}
        self._fired_queue: list[CronJob] = []
        self._last_fired: dict[str, str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for job in self.storage.load_cron(self.project_id):
            if validate_cron(job.cron) is None:
                self._jobs[job.job_id] = job

    def _persist_durable(self) -> None:
        durable = [j for j in self._jobs.values() if j.durable]
        self.storage.save_cron(self.project_id, durable)

    def schedule(self, cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True,
                 job_id: str | None = None) -> tuple[CronJob | None, str | None]:
        err = validate_cron(cron)
        if err:
            return None, err
        self._ensure_loaded()
        job = CronJob(
            job_id=job_id or f"cron_{random.randint(0, 999999):06d}",
            cron=cron, prompt=prompt, recurring=recurring, durable=durable)
        with self._lock:
            self._jobs[job.job_id] = job
        if durable:
            self._persist_durable()
        return job, None

    def cancel(self, job_id: str) -> str:
        self._ensure_loaded()
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return f"Job {job_id} not found"
        if job.durable:
            self._persist_durable()
        return f"Cancelled {job_id}"

    def list_jobs(self) -> list[CronJob]:
        self._ensure_loaded()
        with self._lock:
            return list(self._jobs.values())

    def tick(self, now: Optional[datetime] = None) -> None:
        """Check all jobs against the current time and enqueue matches."""
        self._ensure_loaded()
        now = now or datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            for job in list(self._jobs.values()):
                try:
                    if (cron_matches(job.cron, now)
                            and self._last_fired.get(job.job_id) != marker):
                        self._fired_queue.append(job)
                        self._last_fired[job.job_id] = marker
                        if not job.recurring:
                            self._jobs.pop(job.job_id, None)
                            if job.durable:
                                self._persist_durable()
                except Exception:
                    continue

    def consume_fired(self) -> list[CronJob]:
        """Drain the fire queue. AgentLoop calls this each iteration."""
        with self._lock:
            fired = list(self._fired_queue)
            self._fired_queue.clear()
        return fired

    # Testing helpers
    def _force_fire(self, job_id: str) -> None:
        """Inject a job into the fire queue bypassing the time check."""
        self._ensure_loaded()
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                self._fired_queue.append(job)
