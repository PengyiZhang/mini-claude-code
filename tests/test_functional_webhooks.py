"""F7.2 — WebhookRegistry persistence + WebhookDispatcher fan-out."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.sharing.webhooks import (  # noqa: E402
    WebhookDispatcher, WebhookRegistry, sign_payload,
)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """SSRF validation (P0-A) calls socket.getaddrinfo for hostname URLs.
    Tests in this file don't exercise real DNS — stub it to a public IP
    so the validator passes for any non-literal-IP hostname. Tests that
    care about SSRF rejection explicitly override this fixture."""
    monkeypatch.setattr(
        "mini_cc.sharing.webhooks._resolve_host",
        lambda host: "93.184.216.34")


def test_registry_add_persists_and_lists(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    h = reg.add("https://example.com/hook", ["tool_use", "text"])
    assert h.id.startswith("wh_")
    assert h.url == "https://example.com/hook"
    # Reload from disk → persistence works.
    reg2 = WebhookRegistry(tmp_path, "p1")
    assert len(reg2.list()) == 1
    assert reg2.list()[0].id == h.id


def test_registry_remove(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    h = reg.add("https://x.io/h", [])
    assert reg.remove(h.id) is True
    assert reg.remove(h.id) is False
    assert reg.list() == []


def test_registry_rejects_bad_url(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    try:
        reg.add("not-a-url", [])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    try:
        reg.add("ftp://nope", [])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-http scheme")


def test_matches_filters_by_event_type(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    all_hook = reg.add("https://a.io", [])
    tool_hook = reg.add("https://b.io", ["tool_use"])
    text_hook = reg.add("https://c.io", ["text"])
    m = reg.matches("tool_use")
    urls = {h.url for h in m}
    assert urls == {all_hook.url, tool_hook.url}
    m2 = reg.matches("todos_updated")
    assert {h.url for h in m2} == {all_hook.url}
    m3 = reg.matches("text")
    assert {h.url for h in m3} == {all_hook.url, text_hook.url}


def test_sign_payload_roundtrip():
    secret = "abc"
    body = b'{"x":1}'
    sig = sign_payload(body, secret)
    # 64-char hex string for SHA256
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_dispatcher_fires_only_for_matching_hooks(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    fired: list[tuple[str, bytes, str]] = []
    lock = threading.Lock()

    def fake_deliverer(url: str, body: bytes, secret: str) -> None:
        with lock:
            fired.append((url, body, secret))

    reg.add("https://tool.io", ["tool_use"])
    reg.add("https://all.io", [])

    inner_calls: list[dict] = []
    disp = WebhookDispatcher(reg, "p1", "s1",
                             inner=lambda e: inner_calls.append(e),
                             deliverer=fake_deliverer)
    disp({"type": "text", "text": "hello"})
    # Allow background threads to run.
    time.sleep(0.05)
    urls_fired = {f[0] for f in fired}
    # 'text' event → only the all-events hook matches.
    assert urls_fired == {"https://all.io"}
    # Inner callback was still invoked synchronously.
    assert inner_calls and inner_calls[0]["type"] == "text"
    # Body envelope shape.
    body = json.loads(fired[0][1].decode("utf-8"))
    assert body["project_id"] == "p1"
    assert body["session_id"] == "s1"
    assert body["event"]["type"] == "text"
    assert "ts" in body


def test_dispatcher_no_hooks_skips_delivery(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    fired: list = []
    disp = WebhookDispatcher(reg, "p1", "s1",
                             deliverer=lambda u, b, s: fired.append((u, b, s)))
    disp({"type": "text", "text": "x"})
    time.sleep(0.02)
    assert fired == []


def test_dispatcher_ignores_event_without_type(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    reg.add("https://x.io", [])
    fired: list = []
    disp = WebhookDispatcher(reg, "p1", "s1",
                             deliverer=lambda u, b, s: fired.append(u))
    disp({"foo": "bar"})
    time.sleep(0.02)
    assert fired == []


def test_registry_rejects_ssrf_targets(tmp_path: Path, monkeypatch):
    """P0-A regression: webhook URLs must not allow internal / loopback /
    link-local / reserved targets, otherwise a tenant could use webhook
    delivery to scan the internal network, hit cloud metadata endpoints
    (169.254.169.254), or probe localhost services."""
    reg = WebhookRegistry(tmp_path, "p1")

    bad_targets = [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost/",                          # loopback
        "http://127.0.0.1/",                          # loopback
        "http://10.0.0.1/",                           # private RFC1918
        "http://192.168.1.1/",                        # private RFC1918
        "http://172.16.0.1/",                         # private RFC1918
        "http://[::1]/",                              # IPv6 loopback
        "http://0.0.0.0/",                            # reserved
    ]
    for url in bad_targets:
        try:
            reg.add(url, [])
        except ValueError:
            continue
        raise AssertionError(f"SSRF target must be rejected: {url}")

    # Public hostname resolves to public IP → allowed.
    monkeypatch.setattr(
        "mini_cc.sharing.webhooks._resolve_host",
        lambda host: "93.184.216.34")
    h = reg.add("https://example.com/hook", [])
    assert h.url == "https://example.com/hook"


def test_registry_rejects_url_with_dns_pointing_to_private(tmp_path: Path, monkeypatch):
    """Even if the hostname looks public, if DNS resolves to a private
    range we reject — defense against DNS rebinding."""
    reg = WebhookRegistry(tmp_path, "p1")
    monkeypatch.setattr(
        "mini_cc.sharing.webhooks._resolve_host",
        lambda host: "10.1.2.3")
    try:
        reg.add("https://looks-public.example.com/", [])
    except ValueError:
        return
    raise AssertionError("DNS-to-private must be rejected")


def test_dispatcher_deliverer_exception_is_swallowed(tmp_path: Path):
    reg = WebhookRegistry(tmp_path, "p1")
    reg.add("https://x.io", [])

    def boom(url, body, secret):
        raise RuntimeError("network down")

    disp = WebhookDispatcher(reg, "p1", "s1", deliverer=boom)
    # Should not raise even though deliverer blows up.
    disp({"type": "text", "text": "x"})
    time.sleep(0.05)


def test_session_manager_installs_dispatcher_for_project_webhooks(tmp_path: Path):
    """F7.2 wire-up regression: starting a session must wrap on_event in
    WebhookDispatcher when the project has a webhook registry. Before the
    fix, the dispatcher existed but was never instantiated — the test
    suite passed because every dispatcher test injected one directly.
    """
    from mini_cc.projects import ProjectManager
    from mini_cc.session import SessionManager
    from mini_cc.sharing.webhooks import WebhookDispatcher

    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="p1")
    project = pm.get("p1")
    # Registry must be cached on the Project (not None for FS-backed storage).
    assert project.webhooks is not None
    project.webhooks.add("https://hook.example.com", ["tool_use"])

    captured: list[dict] = []
    sm = SessionManager(pm)
    sm.start_session("p1", "sess1", on_event=lambda e: captured.append(e))
    sess = sm.get("p1", "sess1")
    # The loop's on_event must be a WebhookDispatcher wrapping the inner
    # callback — not the raw lambda we passed in.
    assert isinstance(sess.loop.on_event, WebhookDispatcher)
    # Inner callback still fires synchronously.
    sess.loop.on_event({"type": "tool_use", "name": "x"})
    assert captured and captured[0]["type"] == "tool_use"


def test_session_manager_skips_dispatch_when_no_registry(tmp_path: Path):
    """When the project has no webhook registry (e.g. non-FS storage in
    future), on_event must be passed through unchanged — zero overhead
    and no spurious dispatcher object."""
    from mini_cc.projects import ProjectManager
    from mini_cc.session import SessionManager
    from mini_cc.sharing.webhooks import WebhookDispatcher

    pm = ProjectManager(tmp_path / "projects")
    pm.create(tenant_id="t1", project_id="p1")
    # Simulate a non-FS backend by clearing the cached registry.
    project = pm.get("p1")
    project.webhooks = None

    inner = lambda e: None
    sm = SessionManager(pm)
    sm.start_session("p1", "sess1", on_event=inner)
    sess = sm.get("p1", "sess1")
    # No registry → no wrapping.
    assert sess.loop.on_event is inner
    assert not isinstance(sess.loop.on_event, WebhookDispatcher)
