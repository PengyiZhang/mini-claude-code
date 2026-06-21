"""Phase C: PermissionInterceptor unit tests."""
from __future__ import annotations

import threading
import time

import pytest

from mini_cc.core.permissions import PermissionInterceptor


def test_create_registers_and_lists():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {"command": "ls"})
    assert req.request_id
    assert req.session_id == "sess1"
    assert req.tool_name == "bash"
    assert req.created_at
    pending = pi.list_pending("sess1")
    assert len(pending) == 1
    assert pending[0].request_id == req.request_id


def test_list_pending_filters_by_session():
    pi = PermissionInterceptor(timeout_seconds=10)
    pi.create("sess1", "bash", {})
    pi.create("sess2", "bash", {})
    assert len(pi.list_pending("sess1")) == 1
    assert len(pi.list_pending("sess2")) == 1
    assert len(pi.list_pending()) == 2


def test_decide_allow_wakes_waiter():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})

    result = {}

    def _waiter():
        result["req"] = pi.wait(req.request_id)

    t = threading.Thread(target=_waiter)
    t.start()
    # Let the waiter land in wait().
    time.sleep(0.1)
    assert pi.decide(req.request_id, "allow") is True
    t.join(timeout=2)
    assert not t.is_alive()
    decided = result["req"]
    assert decided is not None
    assert decided.decision == "allow"
    assert decided.deny_message is None


def test_decide_deny_carries_message():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})
    assert pi.decide(req.request_id, "deny", message="user said no") is True
    # After decide, the request is removed from pending.
    assert pi.list_pending("sess1") == []
    # The waiter (if any) would see decision + message on the returned req.
    # Re-construct via wait + decide in another test.


def test_decide_unknown_returns_false():
    pi = PermissionInterceptor(timeout_seconds=10)
    assert pi.decide("nonexistent", "allow") is False


def test_decide_already_decided_returns_false():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})
    assert pi.decide(req.request_id, "allow") is True
    # Second decide on the same id should fail (already gone from registry).
    assert pi.decide(req.request_id, "deny") is False


def test_decide_invalid_value_raises():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})
    with pytest.raises(ValueError):
        pi.decide(req.request_id, "maybe")


def test_wait_returns_none_on_timeout():
    pi = PermissionInterceptor(timeout_seconds=1)
    req = pi.create("sess1", "bash", {})
    start = time.monotonic()
    result = pi.wait(req.request_id, poll_interval=0.1)
    elapsed = time.monotonic() - start
    assert result is None
    assert 0.9 <= elapsed <= 2.5  # roughly the timeout
    # Request was popped from registry.
    assert pi.list_pending("sess1") == []


def test_wait_returns_none_for_unknown_request():
    pi = PermissionInterceptor(timeout_seconds=10)
    assert pi.wait("nonexistent") is None


def test_cancel_unblocks_waiter_as_deny():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})

    result = {}

    def _waiter():
        result["req"] = pi.wait(req.request_id)

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.1)
    pi.cancel(req.request_id)
    t.join(timeout=2)
    assert not t.is_alive()
    decided = result["req"]
    assert decided is not None
    assert decided.decision == "deny"
    assert "cancelled" in (decided.deny_message or "")


def test_wait_stop_event_short_circuits():
    pi = PermissionInterceptor(timeout_seconds=10)
    req = pi.create("sess1", "bash", {})
    stop = threading.Event()

    result = {}

    def _waiter():
        result["req"] = pi.wait(req.request_id, stop_event=stop,
                                poll_interval=0.1)

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["req"] is None  # bailed because stop_event was set
