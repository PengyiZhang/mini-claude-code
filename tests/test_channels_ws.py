"""WS-mode channel receiver tests.

Covers:
1. ``ChannelReceiverSupervisor`` lifecycle (idempotent start, scoped stops).
2. ``ProjectManager._assemble`` spawn point (ws bindings start, webhook don't).
3. HTTP ``POST/DELETE /channels`` → supervisor wiring + transport validation.
4. ``FeishuWsChannel`` construction + event parsing (SDK mocked — no real WS).

The lark-oapi SDK is optional in the test env, so we mock it via
``sys.modules`` patching rather than depending on the real package.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.channels import (  # noqa: E402
    ChannelBinding,
    ChannelRegistry,
    _ensure_feishu_loaded,
    get_supervisor,
    register_channel_kind,
    reset_for_test,
    supports_transport,
)
from mini_cc.channels.base import ChannelBinding as _CB  # noqa: E402
from mini_cc.channels import supervisor as supervisor_mod  # noqa: E402

_ensure_feishu_loaded()


# ── Test fixtures ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_supervisor():
    """Each test gets an isolated supervisor so receivers don't leak
    between tests. ``reset_for_test`` stops the old one and swaps in a
    fresh singleton."""
    sup = reset_for_test()
    yield sup
    sup.stop_all()


def _binding(transport="ws", binding_id="chan_t1", kind="feishu",
             config=None) -> ChannelBinding:
    return ChannelBinding(
        id=binding_id, kind=kind,
        config=config or {"app_id": "a", "app_secret": "b"},
        session_id=None, event_types=[], created_at="",
        transport=transport,
    )


class _FakeProject:
    """Minimal Project stand-in — only the attributes the supervisor
    touches (``meta.tenant_id``, ``project_id``, ``channels``)."""

    def __init__(self, tid, pid, reg):
        self.project_id = pid
        self.meta = types.SimpleNamespace(tenant_id=tid)
        self.channels = reg


class _FakeWsChannel:
    """Test double recording every start/stop call. Mimics the
    ``FeishuWsChannel`` lifecycle API the supervisor depends on."""

    kind = "feishu"
    supported_transports = ("ws",)

    def __init__(self, binding):
        self.binding = binding
        self.start_calls = 0
        self.stop_calls = 0
        self.last_on_inbound = None
        self._running = False

    def start_receiver(self, on_inbound):
        self.start_calls += 1
        self.last_on_inbound = on_inbound
        self._running = True

    def stop_receiver(self):
        self.stop_calls += 1
        self._running = False

    @property
    def receiver_running(self):
        return self._running


def _install_fake_ws_factory(monkeypatch, channel_cls=_FakeWsChannel,
                            captures: dict | None = None):
    """Replace the feishu factory with one that returns ``channel_cls``
    instances. Captures bindings-by-id in ``captures`` if provided.

    Uses ``monkeypatch.setitem`` on the global ``_CHANNEL_KINDS`` dict
    so the real feishu factory is automatically restored at teardown —
    otherwise the fake (which lacks handle_inbound) leaks into other
    test modules via shared process-wide state."""
    from mini_cc.channels import feishu as feishu_mod
    from mini_cc.channels.base import _CHANNEL_KINDS

    def _factory(binding):
        inst = channel_cls(binding)
        if captures is not None:
            captures[binding.id] = inst
        return inst

    # Replace both the module-level factory and the registry entry so
    # ``ChannelRegistry.get_channel`` dispatches through our fake.
    monkeypatch.setattr(feishu_mod, "_factory", _factory)
    monkeypatch.setitem(_CHANNEL_KINDS, "feishu",
                        (_factory, ("ws", "webhook")))


# ── Supervisor lifecycle ────────────────────────────────────────────────

def test_start_for_returns_false_when_transport_not_ws(fresh_supervisor,
                                                        monkeypatch):
    """Webhook bindings are invisible to the supervisor — start_for
    short-circuits before building any channel."""
    _install_fake_ws_factory(monkeypatch)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    b = reg.add("feishu", {"app_id": "a"}, transport="webhook")
    assert fresh_supervisor.start_for(project, b) is False


def test_start_for_returns_false_without_session_manager(fresh_supervisor,
                                                         monkeypatch):
    """When ``attach_session_manager`` hasn't been called (e.g. unit
    tests, or before lifespan completes) start_for is a no-op so callers
    don't have to defensively guard."""
    _install_fake_ws_factory(monkeypatch)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    b = reg.add("feishu", {"app_id": "a"}, transport="ws")
    assert fresh_supervisor.start_for(project, b) is False


def test_start_for_starts_receiver_when_sm_attached(fresh_supervisor,
                                                    monkeypatch):
    captures: dict = {}
    _install_fake_ws_factory(monkeypatch, captures=captures)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    fresh_supervisor.attach_session_manager(_FakeSm())
    b = reg.add("feishu", {"app_id": "a"}, transport="ws")
    assert fresh_supervisor.start_for(project, b) is True
    chan = captures[b.id]
    assert chan.start_calls == 1
    assert chan.receiver_running


def test_start_for_is_idempotent_restarts_existing(fresh_supervisor,
                                                   monkeypatch):
    """Calling start_for twice with the same key stops the old receiver
    before starting the new one — so binding edits get a clean restart
    rather than spawning duplicate threads."""
    captures: dict = {}
    _install_fake_ws_factory(monkeypatch, captures=captures)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    fresh_supervisor.attach_session_manager(_FakeSm())
    b = reg.add("feishu", {"app_id": "a"}, transport="ws")

    fresh_supervisor.start_for(project, b)
    first_chan = captures[b.id]
    fresh_supervisor.start_for(project, b)
    second_chan = captures[b.id]
    # Same binding → same key → first receiver stopped, then a fresh
    # channel instance got started.
    assert first_chan.stop_calls == 1
    assert second_chan.start_calls == 1
    assert second_chan is not first_chan


def test_stop_for_unknown_key_is_noop(fresh_supervisor):
    """stop_for must not raise on unknown keys — delete route calls it
    unconditionally."""
    fresh_supervisor.stop_for("nope", "nope", "chan_nope")


def test_stop_for_stops_single_receiver(fresh_supervisor, monkeypatch):
    captures: dict = {}
    _install_fake_ws_factory(monkeypatch, captures=captures)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    fresh_supervisor.attach_session_manager(_FakeSm())
    b = reg.add("feishu", {"app_id": "a"}, transport="ws")
    fresh_supervisor.start_for(project, b)
    fresh_supervisor.stop_for("t1", "p1", b.id)
    assert captures[b.id].stop_calls == 1


def test_stop_project_stops_only_that_project(fresh_supervisor, monkeypatch):
    """stop_project filters by (tenant_id, project_id); other projects'
    receivers stay alive (e.g. invalidate on one project doesn't kill
    another tenant's bindings)."""
    captures: dict = {}
    _install_fake_ws_factory(monkeypatch, captures=captures)
    reg_a = ChannelRegistry(_tmp_path(), "pa")
    reg_b = ChannelRegistry(_tmp_path(), "pb")
    proj_a = _FakeProject("t1", "pa", reg_a)
    proj_b = _FakeProject("t1", "pb", reg_b)
    fresh_supervisor.attach_session_manager(_FakeSm())
    ba = reg_a.add("feishu", {"app_id": "a"}, transport="ws")
    bb = reg_b.add("feishu", {"app_id": "b"}, transport="ws")
    fresh_supervisor.start_for(proj_a, ba)
    fresh_supervisor.start_for(proj_b, bb)
    fresh_supervisor.stop_project("t1", "pa")
    assert captures[ba.id].stop_calls == 1
    assert captures[bb.id].stop_calls == 0
    assert captures[bb.id].receiver_running


def test_stop_all_clears_every_receiver(fresh_supervisor, monkeypatch):
    captures: dict = {}
    _install_fake_ws_factory(monkeypatch, captures=captures)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    fresh_supervisor.attach_session_manager(_FakeSm())
    for i in range(3):
        b = reg.add("feishu", {"app_id": str(i)}, transport="ws")
        fresh_supervisor.start_for(project, b)
    fresh_supervisor.stop_all()
    assert all(c.stop_calls == 1 for c in captures.values())
    assert fresh_supervisor.status() == []


def test_status_reports_running_state(fresh_supervisor, monkeypatch):
    _install_fake_ws_factory(monkeypatch)
    reg = ChannelRegistry(_tmp_path(), "p1")
    project = _FakeProject("t1", "p1", reg)
    fresh_supervisor.attach_session_manager(_FakeSm())
    b = reg.add("feishu", {"app_id": "a"}, transport="ws")
    fresh_supervisor.start_for(project, b)
    rows = fresh_supervisor.status()
    assert len(rows) == 1
    assert rows[0]["channel_id"] == b.id
    assert rows[0]["running"] is True


def _tmp_path():
    """Fresh tmp dir per call. Using pytest's tmp_path is per-test, but
    we may construct multiple registries in one test."""
    import tempfile
    return __import__("pathlib").Path(tempfile.mkdtemp())


class _FakeSm:
    """Stand-in SessionManager — supervisor only needs ``send`` for the
    inbound callback, but tests here don't trigger inbound so it can be
    a bare object."""


# ── FeishuWsChannel unit tests (SDK mocked) ─────────────────────────────

def test_feishu_ws_channel_constructor_does_not_require_sdk():
    """Constructor must succeed even when lark-oapi isn't installed —
    the import is deferred to ``start_receiver`` so the registry can
    build a placeholder for ws bindings on deployments without the SDK
    (the supervisor will skip start_for with a clear log)."""
    from mini_cc.channels.feishu_ws import FeishuWsChannel
    chan = FeishuWsChannel(_binding(config={"app_id": "a", "app_secret": "b"}))
    assert chan.kind == "feishu"
    assert chan.supported_transports == ("ws",)
    assert chan.receiver_running is False


def test_start_receiver_raises_without_sdk(monkeypatch):
    """When lark-oapi is unavailable, start_receiver raises RuntimeError
    with an actionable message instead of a confusing AttributeError."""
    from mini_cc.channels import feishu_ws
    monkeypatch.setattr(feishu_ws, "_HAS_LARK", False)
    chan = feishu_ws.FeishuWsChannel(
        _binding(config={"app_id": "a", "app_secret": "b"}))
    with pytest.raises(RuntimeError, match="lark-oapi"):
        chan.start_receiver(lambda _t, _m: None)


def test_start_receiver_raises_without_credentials(monkeypatch):
    """Missing app_id/app_secret is a config error, not an SDK error —
    surface it before we even try to construct the client."""
    from mini_cc.channels import feishu_ws
    monkeypatch.setattr(feishu_ws, "_HAS_LARK", True)
    chan = feishu_ws.FeishuWsChannel(_binding(
        config={"app_id": "", "app_secret": ""}))
    with pytest.raises(RuntimeError, match="app_id"):
        chan.start_receiver(lambda _t, _m: None)


def test_start_receiver_spawns_thread_with_sdk(monkeypatch):
    """With the SDK + creds in place, start_receiver builds the
    dispatcher + client and starts the worker thread. We mock the SDK
    so no real WS connection is attempted."""
    from mini_cc.channels import feishu_ws

    fake_lark = _build_fake_lark_sdk()
    monkeypatch.setattr(feishu_ws, "lark", fake_lark)
    monkeypatch.setattr(feishu_ws, "_HAS_LARK", True)

    chan = feishu_ws.FeishuWsChannel(
        _binding(config={"app_id": "id_x", "app_secret": "sec_y"}))
    chan.start_receiver(lambda _t, _m: None)
    try:
        # Client constructed with the right creds.
        assert fake_lark._last_client.args == ("id_x", "sec_y")
        # Worker thread spawned + marked running.
        assert chan._thread is not None
        assert chan._thread.daemon
        assert chan.receiver_running
    finally:
        chan.stop_receiver()


def test_run_swaps_lark_ws_module_loop_to_fresh_loop(monkeypatch):
    """Regression: lark_oapi/ws/client.py:32 captures asyncio.get_event_loop()
    at module import time. Under FastAPI/uvicorn that's the running main
    loop, so loop.run_until_complete() in the worker thread raises "This
    event loop is already running". Verify our _run() overrides the SDK's
    module-level `loop` with a fresh one bound to the worker thread."""
    import asyncio

    from mini_cc.channels import feishu_ws

    fake_lark = _build_fake_lark_sdk()
    monkeypatch.setattr(feishu_ws, "lark", fake_lark)
    monkeypatch.setattr(feishu_ws, "_HAS_LARK", True)

    # Fake the SDK's `lark_oapi.ws.client` module so our sys.modules
    # lookup in _run() finds it. Sentinel value proves the swap occurred.
    fake_ws_client = types.ModuleType("lark_oapi.ws.client")
    fake_ws_client.loop = "SENTINEL_MAIN_LOOP"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", fake_ws_client)

    captured = {}

    def _probe_start():
        captured["loop_after_swap"] = fake_ws_client.loop

    chan = feishu_ws.FeishuWsChannel(
        _binding(config={"app_id": "id_x", "app_secret": "sec_y"}))
    chan.start_receiver(lambda _t, _m: None)
    # start_receiver launched the thread with the original blocking fake
    # client. Swap its start() to our probe + restart the thread so the
    # probe actually runs.
    assert fake_lark._last_client is not None
    fake_lark._last_client.start = _probe_start  # type: ignore[attr-defined]
    chan.stop_receiver()
    if chan._thread is not None:
        chan._thread.join(timeout=1.0)
    fake_ws_client.loop = "SENTINEL_MAIN_LOOP"  # type: ignore[attr-defined]
    chan._stop.clear()
    chan._thread = threading.Thread(
        target=chan._run, daemon=True, name="test-feishu-ws-probe")
    chan._thread.start()

    try:
        chan._thread.join(timeout=2.0)
        assert "loop_after_swap" in captured, \
            "worker thread did not reach _client.start()"
        assert captured["loop_after_swap"] != "SENTINEL_MAIN_LOOP"
        assert isinstance(captured["loop_after_swap"],
                          asyncio.AbstractEventLoop)
        assert not captured["loop_after_swap"].is_running()
    finally:
        chan.stop_receiver()
        if chan._thread is not None:
            chan._thread.join(timeout=1.0)


def test_stop_receiver_sets_stop_event():
    """stop_receiver doesn't (can't) actually kill the WS connection,
    but it must set the stop event so the worker loop bails on the
    next iteration. ``receiver_running`` then flips to False."""
    from mini_cc.channels import feishu_ws
    chan = feishu_ws.FeishuWsChannel(
        _binding(config={"app_id": "a", "app_secret": "b"}))
    chan._stop.clear()
    chan.stop_receiver()
    assert chan._stop.is_set()


def test_handle_inbound_url_verification_handshake_only():
    """WS-mode bindings shouldn't accept inbound webhooks — but the
    public endpoint still resolves them, so url_verification pings
    during misconfiguration get a correct echo. Real messages no-op."""
    from mini_cc.channels import feishu_ws
    chan = feishu_ws.FeishuWsChannel(_binding())
    body = json.dumps({"type": "url_verification",
                       "challenge": "abc"}).encode("utf-8")
    result = chan.handle_inbound(body, {})
    assert result.verification_response == {"challenge": "abc"}

    # Real-looking event → dropped (WS path delivered it separately).
    body2 = json.dumps({
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {"open_id": "o"}},
                  "message": {"message_type": "text",
                              "content": "{\"text\":\"hi\"}"}},
    }).encode("utf-8")
    result2 = chan.handle_inbound(body2, {})
    assert result2.user_input is None
    assert result2.verification_response is None


def test_on_message_receive_invokes_on_inbound_callback():
    """SDK callback converts the event via ``_sdk_event_to_dict`` and
    invokes the supervisor-provided callback with ``(text, metadata)``."""
    from mini_cc.channels import feishu_ws

    received = []
    chan = feishu_ws.FeishuWsChannel(_binding())
    chan._on_inbound = lambda text, meta: received.append((text, meta))

    # SDK event shape: outer object has ``.event`` (the inner event
    # dict) + ``.header``. ``_on_message_receive`` extracts ``event.event``
    # then converts it via ``_sdk_event_to_dict``.
    inner = types.SimpleNamespace(
        sender=types.SimpleNamespace(
            sender_id=types.SimpleNamespace(open_id="ou_1")),
        message=types.SimpleNamespace(
            message_id="m1", chat_id="oc_1", chat_type="p2p",
            message_type="text",
            content=json.dumps({"text": "hello ws"})),
    )
    sdk_event = types.SimpleNamespace(event=inner, header=None)
    chan._on_message_receive(sdk_event)
    # The first call should carry the parsed text + a metadata dict.
    assert received
    text, meta = received[0]
    assert text == "hello ws"
    assert meta.get("chat_id") == "oc_1"
    assert meta.get("sender_open_id") == "ou_1"


def test_on_message_receive_remembers_chat_id_for_deliver_fallback():
    """Regression: a binding with empty chat_id (inbound-only) should
    still push replies — to the chat the inbound message came from.
    Verifies _on_message_receive populates _last_chat_id and deliver
    uses it as fallback target."""
    from mini_cc.channels import feishu_ws

    chan = feishu_ws.FeishuWsChannel(
        _binding(config={"app_id": "a", "app_secret": "b"}))
    assert chan.chat_id == ""
    assert chan._last_chat_id is None

    # Simulate an inbound event carrying chat_id="oc_from_inbound".
    inner = types.SimpleNamespace(
        sender=types.SimpleNamespace(
            sender_id=types.SimpleNamespace(open_id="ou_x")),
        message=types.SimpleNamespace(
            message_id="m1", chat_id="oc_from_inbound", chat_type="group",
            message_type="text",
            content=json.dumps({"text": "hi"})),
    )
    chan._on_inbound = lambda _t, _m: None
    chan._on_message_receive(types.SimpleNamespace(
        event=inner, header=None))

    assert chan._last_chat_id == "oc_from_inbound"

    # deliver() should now POST to oc_from_inbound even though the
    # binding's chat_id config is empty.
    posted = []
    real_post = feishu_ws.requests.post if hasattr(feishu_ws, "requests") \
        else None

    import requests as _real_requests  # type: ignore

    def _fake_post(url, *args, **kwargs):
        posted.append((url, kwargs.get("json")))
        class _R:
            status_code = 200
        return _R()
    # Patch the requests symbol used inside deliver()'s deferred import.
    import mini_cc.channels.feishu_ws as _fws_mod
    orig_requests = sys.modules.get("requests")
    sys.modules["requests"] = types.SimpleNamespace(post=_fake_post)
    fake_token_cache = types.SimpleNamespace(get=lambda: "tok_x")
    chan._token_cache = fake_token_cache  # type: ignore[assignment]
    try:
        chan.deliver({"type": "text", "text": "reply!"})
    finally:
        if orig_requests is not None:
            sys.modules["requests"] = orig_requests
        else:
            del sys.modules["requests"]
    assert len(posted) == 1
    url, payload = posted[0]
    assert payload["receive_id"] == "oc_from_inbound"
    assert "reply!" in payload["content"]


def test_sdk_event_to_dict_handles_nested_objects():
    """``_sdk_event_to_dict`` walks one level of ``vars()`` — verify
    the conversion so changes to the SDK domain shape surface here
    rather than at runtime."""
    from mini_cc.channels.feishu_ws import _sdk_event_to_dict
    obj = types.SimpleNamespace(
        a=1,
        b=types.SimpleNamespace(c=2, d=3),
        e=[1, 2, 3],
    )
    out = _sdk_event_to_dict(obj)
    assert out["a"] == 1
    assert out["b"] == {"c": 2, "d": 3}
    assert out["e"] == [1, 2, 3]


def _build_fake_lark_sdk():
    """Build a fake ``lark_oapi`` module surface sufficient for
    ``FeishuWsChannel.start_receiver`` to construct a client + dispatcher.

    We avoid monkeypatching the real SDK by hand-rolling a small mock
    that records the args passed to ``ws.Client`` and exposes the
    EventDispatcherImpl builder chain."""
    fake = types.ModuleType("lark_oapi")
    # Hold the most-recently-constructed client on the module so the
    # test can inspect call args without fishing through closures.
    fake._last_client = None  # type: ignore[attr-defined]

    class _LogLevel:
        INFO = 30
    fake.LogLevel = _LogLevel

    class _WSClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            fake._last_client = self  # type: ignore[attr-defined]
        def start(self):
            # Block forever so the worker thread stays alive; the test
            # stops it via stop_receiver.
            threading.Event().wait()

    class _WSModule:
        Client = _WSClient
    fake.ws = _WSModule()

    class _DispatcherBuilder:
        def __init__(self):
            self._handlers = {}
        def register_p2_im_message_receive_v1(self, fn):
            self._handlers["im.message.receive_v1"] = fn
            return self
        def build(self):
            return self._handlers

    class _EventDispatcherHandler:
        @staticmethod
        def builder(_verification_token, _encrypt_key):
            return _DispatcherBuilder()
    fake.EventDispatcherHandler = _EventDispatcherHandler

    return fake


# ── supports_transport validation ───────────────────────────────────────

def test_supports_transport_known_combinations():
    assert supports_transport("feishu", "ws")
    assert supports_transport("feishu", "webhook")
    assert not supports_transport("feishu", "socket-mode")


# ── inbound persistence ─────────────────────────────────────────────────

def test_enqueue_inbound_turn_persists_events_to_storage(monkeypatch):
    """Regression: inbound-channel turns must persist events to
    events.jsonl so the /events SSE tail stream surfaces them in the
    live UI (mirrors /send path in routes/sessions.py). Without this,
    the UI shows nothing until manual refresh."""
    import types as _types

    from mini_cc.channels import inbound as inbound_mod

    # Fake project + storage: capture every append_session_event call.
    persisted = []

    class _FakeStorage:
        def append_session_event(self, pid, sid, ev):
            persisted.append((pid, sid, ev))
            return len(persisted)

    class _FakeProject:
        project_id = "p1"
        storage = _FakeStorage()

    # Fake SessionManager: yield three events then stop. Verifies the
    # worker drains the generator AND persists each event.
    class _FakeSM:
        def send(self, pid, sid, text):
            for ev in [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "x"},
                {"type": "text", "text": "world"},
            ]:
                yield ev

    monkeypatch.setattr(inbound_mod, "_resolve_default_session",
                        lambda proj, sm: "sess_chan")

    inbound_mod.enqueue_inbound_turn(
        _FakeSM(), _FakeProject(), "sess_chan", "user msg", {})

    # Worker runs in a daemon thread; give it a moment to drain.
    import time
    time.sleep(0.2)

    assert len(persisted) == 3, f"expected 3 persisted events, got {len(persisted)}"
    assert persisted[0][1] == "sess_chan"
    assert persisted[0][2]["text"] == "hello"
    assert persisted[2][2]["text"] == "world"
