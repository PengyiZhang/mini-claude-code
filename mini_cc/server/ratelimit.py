"""Per-tenant token-bucket rate limiter.

Default 60 RPM per tenant, refill 1 token/sec, burst = bucket size.
Override per-tenant via env (see cli.py).

Thread-safe; no persistence by default. For multi-process deployments
pass a ``redis_url`` to ``make_rate_limiter()`` — counters then live
in Redis so every worker sees the same window.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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


class RedisRateLimiter:
    """Redis-backed token bucket.

    Uses one key per tenant holding the INCR counter, with TTL = window.
    Every call increments; if count > limit, deny. The first call sets
    the TTL. This is the classic "fixed window" approach — slight
    over-admission at window edges is acceptable for rate limiting.

    For the token-bucket-equivalent precision use a Lua script; not
    warranted at our scale.
    """

    def __init__(self, *, default_rpm: int, redis_url: str,
                 overrides: dict[str, int] | None = None,
                 key_prefix: str = "mini_cc:rl:"):
        self._default_rpm = max(1, default_rpm)
        self._overrides = dict(overrides or {})
        self._prefix = key_prefix
        self._redis = self._connect(redis_url)

    @staticmethod
    def _connect(redis_url: str):
        # Lazy import so the dep stays optional for in-memory-only deployments.
        import redis  # type: ignore[import]
        client = redis.from_url(redis_url, socket_connect_timeout=1.0,
                                socket_timeout=1.0)
        # Eagerly probe — if Redis is unreachable we want construction to
        # fail so make_rate_limiter() can fall back to in-memory. Without
        # this check the first .allow() call would raise at request time.
        client.ping()
        return client

    def _rpm_for(self, tenant_id: str) -> int:
        return self._overrides.get(tenant_id, self._default_rpm)

    def allow(self, tenant_id: str) -> tuple[bool, float]:
        rpm = self._rpm_for(tenant_id)
        key = f"{self._prefix}{tenant_id}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 60)  # 60s window; aligns with RPM naming
            count, _ = pipe.execute()
        except Exception as e:
            logger.warning(
                "ratelimit.redis_unavailable tenant=%s error=%s — denying",
                tenant_id, e)
            # Fail-closed is wrong for an unavailable dependency: the
            # caller can't retry meaningfully and the user is locked
            # out. Fail-open is also wrong (no limit at all). Best is
            # to surface "service degraded" and let the caller decide.
            raise RuntimeError(
                f"Redis rate-limiter unavailable: {e}") from e
        if count <= rpm:
            return True, 0.0
        # Window resets when TTL expires — that's the worst-case wait.
        return False, 60.0


def make_rate_limiter(*, default_rpm: int,
                      overrides: dict[str, int] | None = None,
                      redis_url: str | None = None,
                      fallback_on_error: bool = True) -> "TenantRateLimiter | RedisRateLimiter":
    """Factory: pick Redis if a URL is configured, fall back to memory
    on connect failure (or if the optional redis dep isn't installed).

    Redis is best-effort. If it's unreachable at construction time we
    log a warning and return an in-memory limiter so the server still
    boots. Multi-process deployments lose shared counters as a result —
    surface via metrics, not a crash.
    """
    if not redis_url:
        return TenantRateLimiter(default_rpm=default_rpm, overrides=overrides)
    try:
        return RedisRateLimiter(
            default_rpm=default_rpm, redis_url=redis_url, overrides=overrides)
    except Exception as e:
        if not fallback_on_error:
            raise
        logger.warning(
            "ratelimit.redis_init_failed url=%s error=%s — falling back "
            "to in-memory limiter. Multi-process deployments will not "
            "share counters.",
            redis_url, e)
        return TenantRateLimiter(default_rpm=default_rpm, overrides=overrides)
