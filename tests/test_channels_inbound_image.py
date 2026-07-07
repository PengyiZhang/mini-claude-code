"""enqueue_inbound_turn, when metadata.kind=image, downloads each image_key,
writes it to project.assets, and pushes a content-blocks user turn into
SessionManager.send.

Two paths covered:
* happy path — download_image returns bytes; assets.put is called with
  src="feishu:{image_key}"; sm.send receives a list content blocks including
  ``{type:image, asset_id}``.
* failure path — download_image raises; the worker degrades gracefully to a
  text placeholder block (no exception bubbles out; the turn still runs so
  the lead can see something went wrong).

Both tests use a threading.Event to synchronize with the worker thread
instead of a fixed ``time.sleep`` — the plan suggested Event join; we use
a "worker done" event set in a finally clause to keep the tests deterministic
on noisy CI machines.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from mini_cc.channels import inbound


def _wait_for_worker(patch_target) -> None:
    """Replace ``_resolve_default_session`` with a wrapper that signals an
    event after the worker has finished its sm.send drain. We hook here
    (rather than inside _worker) because it's the latest point in the
    worker body we can monkeypatch cheaply.

    Actually simpler: monkeypatch sm.send to record + signal after iter
    completes. Done inline per-test below."""
    pass


def test_inbound_turn_with_image_calls_asset_put_and_send(monkeypatch):
    """Happy path: image turn → download_image called per key, bytes go
    through assets.put, sm.send receives a content-blocks list."""
    project = MagicMock()
    project.project_id = "p1"
    project.assets.put.return_value = "asset_id_aaa"

    def fake_download(image_key, cache):
        # Sanity: TokenCache was constructed from binding_config.
        assert cache.app_id == "app_id_x"
        assert cache.app_secret == "app_secret_y"
        return (b"\xff\xd8fake", "image/jpeg")

    monkeypatch.setattr(inbound, "download_image", fake_download)

    done = threading.Event()
    captured = {}

    def fake_send(pid, sid, user_input):
        captured["pid"] = pid
        captured["sid"] = sid
        captured["user_input"] = user_input
        try:
            yield from []
        finally:
            done.set()

    sm = MagicMock()
    sm.send.side_effect = fake_send

    inbound.enqueue_inbound_turn(
        sm, project, "sess-1", "",
        {"kind": "image", "image_keys": ["img_key_x"],
         "source": "feishu",
         "_binding_config": {"app_id": "app_id_x",
                              "app_secret": "app_secret_y"}})

    # Bump from the plan's 0.1s — CI machines (especially Windows + daemon
    # threads) can stutter; 1s ceiling still keeps the suite fast.
    assert done.wait(timeout=1.0), "worker did not invoke sm.send within 1s"

    project.assets.put.assert_called_once_with(
        b"\xff\xd8fake", media_type="image/jpeg", src="feishu:img_key_x")
    user_input = captured["user_input"]
    assert isinstance(user_input, list)
    assert {"type": "image", "asset_id": "asset_id_aaa"} in user_input
    # Empty text user_input should NOT produce a stray empty text block.
    assert not any(b.get("type") == "text" and not b.get("text")
                   for b in user_input if isinstance(b, dict))
    assert captured["pid"] == "p1"
    assert captured["sid"] == "sess-1"


def test_inbound_turn_download_failure_falls_back_to_placeholder(monkeypatch):
    """Failure path: download raises ConnectionError; worker emits a
    text placeholder block instead of crashing."""
    project = MagicMock()
    project.project_id = "p1"
    project.assets.put.return_value = "xxx"

    def boom(image_key, cache):
        raise ConnectionError("timeout")
    monkeypatch.setattr(inbound, "download_image", boom)

    done = threading.Event()
    captured = {}

    def fake_send(pid, sid, user_input):
        captured["user_input"] = user_input
        try:
            yield from []
        finally:
            done.set()

    sm = MagicMock()
    sm.send.side_effect = fake_send

    inbound.enqueue_inbound_turn(
        sm, project, "sess-1", "",
        {"kind": "image", "image_keys": ["bad_key"],
         "source": "feishu",
         "_binding_config": {"app_id": "a", "app_secret": "b"}})

    assert done.wait(timeout=1.0), "worker did not invoke sm.send within 1s"

    user_input = captured["user_input"]
    assert isinstance(user_input, list)
    assert any(isinstance(b, dict) and b.get("type") == "text"
               and "download failed" in b["text"]
               for b in user_input), \
        f"expected a 'download failed' placeholder block in {user_input!r}"
    # assets.put must NOT be called when download raised.
    project.assets.put.assert_not_called()
