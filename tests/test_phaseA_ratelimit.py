"""Phase A: per-tenant token bucket rate limiter."""
from __future__ import annotations

import threading
import time

from mini_cc.server.ratelimit import TenantRateLimiter


def test_default_bucket_allows_rpm_then_denies():
    lim = TenantRateLimiter(default_rpm=3)
    # First 3 consume the bucket
    for i in range(3):
        ok, _ = lim.allow("t1")
        assert ok, f"call {i+1} should be allowed"
    # 4th within window denied
    ok, retry = lim.allow("t1")
    assert not ok
    assert retry > 0


def test_refill_after_wait_allows_again():
    lim = TenantRateLimiter(default_rpm=2)
    lim.allow("t1")
    lim.allow("t1")
    ok, _ = lim.allow("t1")
    assert not ok
    # Wait ~1 token's worth of refill (default_rpm/60 per sec → 30s for 1 token
    # at rpm=2). Use higher rpm so the test runs fast.
    lim2 = TenantRateLimiter(default_rpm=600)  # 10/sec
    lim2.allow("t2")
    lim2.allow("t2")
    # Drain fully
    for _ in range(600):
        lim2.allow("t2")
    ok, _ = lim2.allow("t2")
    assert not ok
    time.sleep(0.25)  # ~2.5 tokens refill
    ok, _ = lim2.allow("t2")
    assert ok


def test_per_tenant_override():
    lim = TenantRateLimiter(default_rpm=2, overrides={"vip": 5})
    for _ in range(5):
        assert lim.allow("vip")[0]
    assert not lim.allow("vip")[0]
    # Default still applies to other tenants
    assert lim.allow("other")[0]
    assert lim.allow("other")[0]
    assert not lim.allow("other")[0]


def test_tenants_are_isolated():
    lim = TenantRateLimiter(default_rpm=2)
    assert lim.allow("a")[0]
    assert lim.allow("a")[0]
    assert not lim.allow("a")[0]
    # Tenant b has its own bucket
    assert lim.allow("b")[0]
    assert lim.allow("b")[0]
    assert not lim.allow("b")[0]


def test_concurrent_allow_does_not_oversell():
    """Hammer the limiter from many threads; total allows must not exceed capacity."""
    lim = TenantRateLimiter(default_rpm=50)
    allowed = 0
    lock = threading.Lock()

    def worker():
        nonlocal allowed
        for _ in range(20):
            ok, _ = lim.allow("cc")
            if ok:
                with lock:
                    allowed += 1

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Capacity is 50; concurrent calls must never exceed it.
    assert allowed <= 50, f"oversold bucket: {allowed} allows"
    assert allowed == 50, f"expected full bucket consumed, got {allowed}"
