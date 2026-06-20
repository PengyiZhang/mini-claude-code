"""Per-tenant token-bucket rate limiter.

Default 60 RPM per tenant, refill 1 token/sec, burst = bucket size.
Override per-tenant via env (see cli.py).

Thread-safe; no persistence — counters reset on server restart, which
matches the in-memory session model.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    """Token bucket. capacity tokens max, refill_rate tokens/sec."""
    capacity: float
    refill_rate: float
    tokens: float
    last_refill: float = field(default_factory=time.monotonic)

    def refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

    def try_consume(self, now: float) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). retry_after is 0.0 on success."""
        self.refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        # Not enough tokens — how long until one is available?
        deficit = 1.0 - self.tokens
        return False, deficit / self.refill_rate if self.refill_rate > 0 else float("inf")


class TenantRateLimiter:
    """In-memory token bucket per tenant.

    Buckets are created lazily on first request. A default RPM applies
    to any tenant without an explicit override.
    """
    def __init__(self, default_rpm: int, overrides: dict[str, int] | None = None):
        self._default_rpm = max(1, default_rpm)
        self._overrides = dict(overrides or {})
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _rpm_for(self, tenant_id: str) -> int:
        return self._overrides.get(tenant_id, self._default_rpm)

    def _bucket_for(self, tenant_id: str) -> _Bucket:
        b = self._buckets.get(tenant_id)
        if b is not None:
            return b
        rpm = self._rpm_for(tenant_id)
        # capacity = rpm (allow a full-minute burst at once), refill = rpm/60 per sec
        b = _Bucket(
            capacity=float(rpm),
            refill_rate=rpm / 60.0,
            tokens=float(rpm),
        )
        self._buckets[tenant_id] = b
        return b

    def allow(self, tenant_id: str) -> tuple[bool, float]:
        """Probe and consume one token for the given tenant.

        Returns (allowed, retry_after_seconds). On deny the caller should
        surface a 429 with ``Retry-After: <ceil(retry_after)>``.
        """
        now = time.monotonic()
        with self._lock:
            return self._bucket_for(tenant_id).try_consume(now)
