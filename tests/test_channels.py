"""Bidirectional channel abstraction tests.

Three groups:
1. ChannelRegistry persistence + lookup semantics (kind-agnostic).
2. FeishuChannel inbound: signature verification, url_verification
   handshake, encrypted envelope, text-message parsing, non-text
   fallback, duplicate-mention stripping.
3. FeishuChannel outbound: token caching/refresh, deliver rendering
   rules, chat_id gating.
4. ChannelDispatcher fan-out: per-event-type routing, channel-kind
   failure isolation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.channels import (  # noqa: E402
    ChannelDispatcher,
    ChannelRegistry,
    InboundResult,
    register_channel_kind,
    registered_kinds,
    _ensure_feishu_loaded,
)
from mini_cc.channels.base import ChannelBinding  # noqa: E402
from mini_cc.channels import feishu as feishu_mod  # noqa: E402

_ensure_feishu_loaded()


# ── Registry ────────────────────────────────────────────────────────────

def test_registry_add_persists_and_lists(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b = reg.add("feishu", {"app_id": "a", "app_secret": "b"},
                session_id="sess-1", event_types=["text"])
    assert b.id.startswith("chan_")
    assert b.kind == "feishu"
    assert b.session_id == "sess-1"
    assert b.event_types == ["text"]

    # Reload from disk: must survive a fresh instance.
    reg2 = ChannelRegistry(tmp_path, "p1")
    loaded = reg2.get(b.id)
    assert loaded is not None
    assert loaded.kind == "feishu"
    assert loaded.config == {"app_id": "a", "app_secret": "b"}
    assert loaded.session_id == "sess-1"
    assert loaded.event_types == ["text"]


def test_registry_remove(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b = reg.add("feishu", {"app_id": "a"})
    assert reg.remove(b.id)
    assert reg.get(b.id) is None
    assert not reg.remove(b.id)


def test_registry_matches_event_filter(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    all_binding = reg.add("feishu", {"app_id": "a"})
    text_only = reg.add("feishu", {"app_id": "b"}, event_types=["text"])
    matched = reg.matches_event("text")
    ids = {b.id for b in matched}
    assert all_binding.id in ids
    assert text_only.id in ids
    # Non-subscribed event_type only hits the all-binding.
    only_all = reg.matches_event("todos_updated")
    assert {b.id for b in only_all} == {all_binding.id}


def test_registry_update_in_place(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b = reg.add("feishu", {"app_id": "a"})
    updated = reg.update(b.id, config={"app_id": "new"},
                        session_id="sess-2", event_types=["text"])
    assert updated is not None
    assert updated.config == {"app_id": "new"}
    assert updated.session_id == "sess-2"
    assert updated.event_types == ["text"]
    # Reload verifies persistence.
    reg2 = ChannelRegistry(tmp_path, "p1")
    loaded = reg2.get(b.id)
    assert loaded.config == {"app_id": "new"}
    assert loaded.session_id == "sess-2"


def test_registry_get_channel_returns_registered_kind(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b = reg.add("feishu", {"app_id": "a", "app_secret": "b"})
    chan = reg.get_channel(b)
    assert chan is not None
    assert chan.kind == "feishu"


def test_registry_get_channel_unknown_kind_returns_none(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b = reg.add("nonexistent_kind", {})
    assert reg.get_channel(b) is None


def test_registered_kinds_includes_feishu():
    kinds = registered_kinds()
    assert "feishu" in kinds


# ── Feishu inbound ──────────────────────────────────────────────────────

def _binding(config=None) -> ChannelBinding:
    return ChannelBinding(
        id="chan_test",
        kind="feishu",
        config=config or {},
        session_id=None,
        event_types=[],
        created_at="",
    )


def test_feishu_url_verification_handshake():
    chan = feishu_mod.FeishuChannel(_binding())
    body = json.dumps({
        "challenge": "abc123",
        "token": "ignored",
        "type": "url_verification",
    }).encode("utf-8")
    result = chan.handle_inbound(body, {})
    assert result.verification_response == {"challenge": "abc123"}
    assert result.user_input is None


def test_feishu_text_message_parses_to_user_input():
    chan = feishu_mod.FeishuChannel(_binding())
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "e1",
            "event_type": "im.message.receive_v1",
            "token": "tok",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user"},
            "message": {
                "message_id": "m1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "你好世界"}),
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    result = chan.handle_inbound(body, {})
    assert result.user_input == "你好世界"
    assert result.metadata.get("chat_id") == "oc_1"
    assert result.metadata.get("sender_open_id") == "ou_1"
    assert result.metadata.get("source") == "feishu"


def test_feishu_non_text_message_falls_back_to_placeholder():
    chan = feishu_mod.FeishuChannel(_binding())
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "t"},
        "event": {
            "sender": {"sender_id": {"open_id": "o"}},
            "message": {
                "message_id": "m",
                "chat_id": "c",
                "message_type": "image",
                "content": "{}",
            },
        },
    }
    result = chan.handle_inbound(json.dumps(payload).encode("utf-8"), {})
    assert result.user_input is not None
    assert "image" in result.user_input


def test_feishu_strips_bot_mention_at_user_tokens():
    chan = feishu_mod.FeishuChannel(_binding())
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "o"}},
            "message": {
                "message_type": "text",
                "chat_id": "c",
                "message_id": "m",
                "content": json.dumps({"text": "@_user_1 hello"}),
            },
        },
    }
    result = chan.handle_inbound(json.dumps(payload).encode("utf-8"), {})
    assert result.user_input == "hello"


def test_feishu_ignores_unrelated_event_types():
    chan = feishu_mod.FeishuChannel(_binding())
    payload = {
        "header": {"event_type": "contact.user.updated_v3"},
        "event": {},
    }
    result = chan.handle_inbound(json.dumps(payload).encode("utf-8"), {})
    assert result.user_input is None
    assert result.verification_response is None


def test_feishu_verification_token_check_rejects_mismatch():
    chan = feishu_mod.FeishuChannel(_binding({"verification_token": "expected"}))
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "WRONG"},
        "event": {
            "sender": {"sender_id": {"open_id": "o"}},
            "message": {"message_type": "text", "content": "{\"text\":\"x\"}"},
        },
    }
    result = chan.handle_inbound(json.dumps(payload).encode("utf-8"), {})
    assert result.user_input is None


def test_feishu_signature_check_rejects_missing_header_when_encrypt_key_set():
    chan = feishu_mod.FeishuChannel(_binding({"encrypt_key": "k"}))
    body = json.dumps({"type": "url_verification", "challenge": "c"}).encode("utf-8")
    # No X-Lark-Signature header → reject.
    result = chan.handle_inbound(body, {})
    assert result.user_input is None
    assert result.verification_response is None


def test_feishu_signature_check_accepts_valid_signature():
    chan = feishu_mod.FeishuChannel(_binding({"encrypt_key": "k"}))
    body = json.dumps({"type": "url_verification", "challenge": "c"}).encode("utf-8")
    ts = "1700000000"
    nonce = "n"
    expected = hashlib.sha256(
        f"{ts}{nonce}k".encode("utf-8") + body).hexdigest()
    headers = {
        "X-Lark-Signature": expected,
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
    }
    result = chan.handle_inbound(body, headers)
    assert result.verification_response == {"challenge": "c"}


def test_feishu_handles_garbled_body_gracefully():
    chan = feishu_mod.FeishuChannel(_binding())
    result = chan.handle_inbound(b"not json", {})
    assert result.user_input is None
    assert result.verification_response is None


# ── Feishu outbound ─────────────────────────────────────────────────────

def test_feishu_deliver_skips_when_no_chat_id(monkeypatch):
    chan = feishu_mod.FeishuChannel(_binding({"app_id": "a", "app_secret": "s"}))
    # No chat_id → short-circuit before any HTTP call.
    called = {"n": 0}

    def _post(*a, **kw):
        called["n"] += 1
        raise AssertionError("should not call requests.post without chat_id")
    monkeypatch.setattr(feishu_mod.requests, "post", _post)
    chan.deliver({"type": "text", "text": "hello"})
    assert called["n"] == 0


def test_feishu_deliver_text_event_posts_message(monkeypatch):
    chan = feishu_mod.FeishuChannel(_binding({
        "app_id": "a", "app_secret": "s", "chat_id": "oc_x"}))
    monkeypatch.setattr(chan, "_get_token", lambda: "tok-123")
    captured = {}

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")

        class _Resp:
            def json(self): return {}
        return _Resp()
    monkeypatch.setattr(feishu_mod.requests, "post", _post)
    chan.deliver({"type": "text", "text": "hello"})
    assert captured["json"]["receive_id"] == "oc_x"
    assert captured["json"]["msg_type"] == "text"
    assert json.loads(captured["json"]["content"]) == {"text": "hello"}
    assert captured["headers"]["Authorization"] == "Bearer tok-123"


def test_feishu_deliver_skips_unsupported_event_types(monkeypatch):
    chan = feishu_mod.FeishuChannel(_binding({"chat_id": "oc"}))
    monkeypatch.setattr(chan, "_get_token", lambda: "t")
    called = {"n": 0}

    def _post(*a, **kw):
        called["n"] += 1
    monkeypatch.setattr(feishu_mod.requests, "post", _post)
    # tool_use is tool noise — should not be delivered.
    chan.deliver({"type": "tool_use", "name": "x"})
    assert called["n"] == 0


def test_feishu_deliver_lead_nudged_renders_items(monkeypatch):
    chan = feishu_mod.FeishuChannel(_binding({"chat_id": "oc"}))
    monkeypatch.setattr(chan, "_get_token", lambda: "t")
    captured = {}

    def _post(url, **kw):
        captured["content"] = kw["json"]["content"]
        class _Resp:
            def json(self): return {}
        return _Resp()
    monkeypatch.setattr(feishu_mod.requests, "post", _post)
    chan.deliver({"type": "lead_nudged",
                  "items": [{"from": "alice", "kind": "milestone"}]})
    assert "alice" in captured["content"]
    assert "milestone" in captured["content"]


def test_feishu_token_caches_across_calls(monkeypatch):
    chan = feishu_mod.FeishuChannel(_binding({
        "app_id": "a", "app_secret": "s", "chat_id": "oc"}))
    calls = {"n": 0}

    def _post(url, **kw):
        if "tenant_access_token" in url:
            calls["n"] += 1

            class _Resp:
                def json(self):
                    return {"tenant_access_token": "tok", "expire": 7200}
            return _Resp()

        class _Resp:
            def json(self): return {}
        return _Resp()
    monkeypatch.setattr(feishu_mod.requests, "post", _post)
    chan.deliver({"type": "text", "text": "m1"})
    chan.deliver({"type": "text", "text": "m2"})
    chan.deliver({"type": "text", "text": "m3"})
    # Token refresh should fire exactly once for three deliveries.
    assert calls["n"] == 1


def test_feishu_token_returns_empty_on_missing_credentials():
    chan = feishu_mod.FeishuChannel(_binding({}))
    assert chan._get_token() == ""


# ── ChannelDispatcher ────────────────────────────────────────────────────

class _FakeChannel:
    """Test double that records every deliver() call."""
    kind = "fake"

    def __init__(self, binding):
        self.binding = binding
        self.events: list[dict] = []

    def deliver(self, event):
        self.events.append(event)

    def handle_inbound(self, body, headers):
        return InboundResult(user_input=body.decode("utf-8"))


def test_dispatcher_routes_to_subscribed_bindings(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b1 = reg.add("fake", {}, event_types=[])
    b2 = reg.add("fake", {}, event_types=["tool_use"])

    inst1 = _FakeChannel(b1)
    inst2 = _FakeChannel(b2)
    instances = {b1.id: inst1, b2.id: inst2}
    disp = ChannelDispatcher(reg, "p1", "s1",
                             channel_factory=lambda b: instances[b.id])
    disp({"type": "text", "text": "hi"})
    # Only b1 subscribes to text (empty filter = all).
    assert len(inst1.events) == 1
    assert len(inst2.events) == 0


def test_dispatcher_failure_does_not_propagate(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    b1 = reg.add("fake", {})

    class _Broken:
        kind = "fake"

        def __init__(self, binding):
            pass

        def deliver(self, event):
            raise RuntimeError("boom")

        def handle_inbound(self, body, headers):
            return InboundResult()

    disp = ChannelDispatcher(reg, "p1", "s1",
                             channel_factory=lambda b: _Broken())
    # Must not raise — failures are swallowed per channel.
    disp({"type": "text"})


def test_dispatcher_calls_inner_callback(tmp_path):
    reg = ChannelRegistry(tmp_path, "p1")
    inner_events = []
    disp = ChannelDispatcher(reg, "p1", "s1",
                             inner=inner_events.append,
                             channel_factory=lambda b: None)
    disp({"type": "text"})
    assert inner_events == [{"type": "text"}]
