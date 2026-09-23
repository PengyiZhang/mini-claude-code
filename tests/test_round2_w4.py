"""W4 — Email subsystem: SMTP/IMAP wrappers + email_wait resolver."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_cc.auth import TenantKeyRegistry
from mini_cc.email import (EmailService, IMAPConfig, InboundEmail,
                            SMTPConfig, matches_filters)
from mini_cc.projects import ProjectManager
from mini_cc.server.app import build_app
from mini_cc.session import SessionManager
from mini_cc.storage import FSStorage
from mini_cc.workflow.workflow_v2 import WorkflowService


AUTH = {"Authorization": "Bearer mck_testkey"}


def _svc(tmp_path) -> WorkflowService:
    return WorkflowService(FSStorage(tmp_path / "state"))


def _park_at_email_wait(svc, project_id="p1", *, from_filter=None,
                         subject_filter=None):
    cfg = {}
    if from_filter:
        cfg["from_filter"] = from_filter
    if subject_filter:
        cfg["subject_filter"] = subject_filter
    d = svc.create_definition(project_id, {
        "name": "wf",
        "steps": [
            {"id": "wait", "type": "email_wait", "config": cfg},
            {"id": "post", "prompt": "post"},
        ],
    })
    run = svc.start_run(project_id, d.def_id)
    run = svc.drive_run(project_id, run.run_id, lambda p, r: "x")
    return d, run


# ── Filter matching ────────────────────────────────────────────────────────

def test_matches_filters_no_constraints():
    e = InboundEmail(from_addr="x@y", to_addr="t", subject="s", body="b")
    assert matches_filters(e) is True


def test_matches_filters_from_substring_case_insensitive():
    e = InboundEmail(from_addr="Alice@Example.COM", to_addr="", subject="", body="")
    assert matches_filters(e, from_filter="@example.com") is True


def test_matches_filters_from_mismatch():
    e = InboundEmail(from_addr="alice@other.io", to_addr="", subject="", body="")
    assert matches_filters(e, from_filter="@example.com") is False


def test_matches_filters_subject_substring():
    e = InboundEmail(from_addr="", to_addr="", subject="Re: [TICKET-42] Help", body="")
    assert matches_filters(e, subject_filter="ticket-42") is True
    assert matches_filters(e, subject_filter="TICKET-99") is False


# ── Service-layer resolver ─────────────────────────────────────────────────

def test_resolve_email_wait_advances_run(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc)
    resolved = svc.resolve_email_wait(
        "p1", run.run_id, "wait",
        {"from_addr": "boss@co.io", "subject": "approved", "body": "go"})
    assert resolved.current_step_idx == 1
    wait_sr = next(s for s in resolved.step_runs if s.step_id == "wait")
    assert wait_sr.status == "completed"
    assert wait_sr.output["email"]["from"] == "boss@co.io"
    assert wait_sr.output["resolver"] == "email"


def test_resolve_email_wait_from_filter_match(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc, from_filter="@company.io")
    resolved = svc.resolve_email_wait(
        "p1", run.run_id, "wait",
        {"from_addr": "Boss@Company.IO", "subject": "x"})
    assert resolved.step_runs[0].status == "completed"


def test_resolve_email_wait_from_filter_mismatch_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc, from_filter="@company.io")
    with pytest.raises(ValueError, match="does not match step filters"):
        svc.resolve_email_wait(
            "p1", run.run_id, "wait",
            {"from_addr": "stranger@evil.io", "subject": "x"})


def test_resolve_email_wait_subject_filter_match(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc, subject_filter="[URGENT]")
    resolved = svc.resolve_email_wait(
        "p1", run.run_id, "wait",
        {"from_addr": "a@b", "subject": "Re: [URGENT] please"})
    assert resolved.step_runs[0].status == "completed"


def test_resolve_email_wait_non_email_step_raises(tmp_path):
    svc = _svc(tmp_path)
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "s1", "prompt": "do"}],
    })
    run = svc.start_run("p1", d.def_id)
    with pytest.raises(ValueError, match="not an email_wait"):
        svc.resolve_email_wait("p1", run.run_id, "s1", {"from_addr": "a@b"})


def test_resolve_email_wait_not_paused_raises(tmp_path):
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc)
    svc.resolve_email_wait("p1", run.run_id, "wait",
                            {"from_addr": "a@b", "subject": "x"})
    with pytest.raises(ValueError, match="not paused"):
        svc.resolve_email_wait("p1", run.run_id, "wait",
                                {"from_addr": "a@b", "subject": "x"})


def test_resolve_email_wait_run_missing_returns_none(tmp_path):
    svc = _svc(tmp_path)
    assert svc.resolve_email_wait("p1", "ghost", "wait", {}) is None


def test_resolve_email_wait_persists(tmp_path):
    s = FSStorage(tmp_path / "state")
    svc = WorkflowService(s)
    d, run = _park_at_email_wait(svc)
    svc.resolve_email_wait("p1", run.run_id, "wait",
                            {"from_addr": "a@b", "subject": "x"})
    reloaded = WorkflowService(s).get_run("p1", run.run_id)
    assert reloaded.step_runs[0].status == "completed"


def test_resolve_email_wait_accepts_from_alias(tmp_path):
    """Email payload can use 'from' (alias) instead of 'from_addr'."""
    svc = _svc(tmp_path)
    d, run = _park_at_email_wait(svc)
    resolved = svc.resolve_email_wait("p1", run.run_id, "wait",
                                       {"from": "alias@x.io", "subject": ""})
    wait_sr = next(s for s in resolved.step_runs if s.step_id == "wait")
    assert wait_sr.output["email"]["from"] == "alias@x.io"


# ── EmailService.send (SMTP) ───────────────────────────────────────────────

class _FakeSMTP:
    """Drop-in smtplib.SMTP replacement for tests."""
    sent: list = []
    def __init__(self):
        self.login_called = False
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def login(self, u, p): self.login_called = True
    def send_message(self, msg):
        type(self).sent.append({"from": msg["From"], "to": msg["To"],
                                "subject": msg["Subject"]})


def test_email_send_uses_injected_smtp_factory():
    cfg = SMTPConfig(host="smtp.example.com", port=587, username="u",
                      password="p", from_addr="from@x.io")
    svc = EmailService(smtp=cfg)
    captured = []
    def factory():
        class Ctx:
            def __enter__(self):
                f = _FakeSMTP()
                captured.append(f)
                return f
            def __exit__(self, *exc): return False
        return Ctx()
    sender = svc.send(to="to@y.io", subject="hi", body="hello",
                       smtp_factory=factory)
    assert sender == "from@x.io"
    assert captured[0].login_called is True
    assert captured[0].sent[0]["to"] == "to@y.io"


def test_email_send_raises_when_smtp_unconfigured():
    svc = EmailService()
    with pytest.raises(RuntimeError, match="SMTP not configured"):
        svc.send(to="x@y", subject="s", body="b")


def test_email_send_requires_from_addr():
    cfg = SMTPConfig(host="x", port=25)
    svc = EmailService(smtp=cfg)
    with pytest.raises(ValueError, match="from_addr"):
        svc.send(to="x@y", subject="s", body="b",
                  smtp_factory=lambda: _FakeSMTP())


# ── EmailService.poll_once (IMAP) ──────────────────────────────────────────

class _FakeIMAPMsg:
    def __init__(self, raw): self.raw = raw


class _FakeIMAP:
    """Drains a fixed list of unseen messages."""
    def __init__(self, messages):
        self.messages = messages
        self.seen_flags: list = []
        self.selected = None
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def login(self, u, p): pass
    def select(self, mailbox): self.selected = mailbox
    def search(self, *args):
        return ("OK", [b" ".join(str(i).encode() for i in range(len(self.messages)))])
    def fetch(self, num, what):
        idx = int(num)
        if idx >= len(self.messages):
            return ("BAD", [None])
        return ("OK", [(_FakeIMAPMsg(self.messages[idx]), self.messages[idx])])
    def store(self, num, op, flag):
        self.seen_flags.append(int(num))


RAW_EMAIL = (
    b"From: alice@example.com\r\n"
    b"To: bot@workflow.io\r\n"
    b"Subject: Re: approve\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"please go ahead\r\n"
)


def test_poll_once_drains_and_marks_seen():
    cfg = IMAPConfig(host="imap.x", username="u", password="p")
    svc = EmailService(imap=cfg)
    fake = _FakeIMAP([RAW_EMAIL])
    msgs = svc.poll_once(imap_factory=lambda: fake)
    assert len(msgs) == 1
    assert msgs[0].from_addr == "alice@example.com"
    assert msgs[0].subject == "Re: approve"
    assert "please go ahead" in msgs[0].body
    assert fake.seen_flags == [0]


def test_poll_once_raises_when_unconfigured():
    svc = EmailService()
    with pytest.raises(RuntimeError, match="IMAP not configured"):
        svc.poll_once()


def test_poll_once_handles_empty_mailbox():
    cfg = IMAPConfig(host="x", username="u", password="p")
    svc = EmailService(imap=cfg)
    fake = _FakeIMAP([])
    msgs = svc.poll_once(imap_factory=lambda: fake)
    assert msgs == []


# ── HTTP route ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    reg = TenantKeyRegistry(tmp_path / "keys.json")
    reg.generate("tenant1")
    import json as _json
    (tmp_path / "keys.json").write_text(
        _json.dumps({"mck_testkey": "tenant1"}))
    pm = ProjectManager(tmp_path / "projects")
    sm = SessionManager(pm)
    app = build_app(data_dir=tmp_path, key_registry=reg, pm=pm, sm=sm)
    c = TestClient(app)
    c.post("/tenants/tenant1/projects",
           headers=AUTH, json={"project_id": "p1"})
    return c


def _park_via_service(client, *, from_filter=None, subject_filter=None):
    pm = client.app.state.pm
    project = pm.get("p1", tenant_id="tenant1")
    svc = project.workflows_v2
    cfg = {}
    if from_filter: cfg["from_filter"] = from_filter
    if subject_filter: cfg["subject_filter"] = subject_filter
    d = svc.create_definition("p1", {
        "name": "wf",
        "steps": [{"id": "wait", "type": "email_wait", "config": cfg}],
    })
    run = svc.start_run("p1", d.def_id)
    svc.drive_run("p1", run.run_id, lambda p, r: "x")
    return run.run_id


def test_http_resolve_email_basic(client):
    run_id = _park_via_service(client)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/email/wait",
        json={"from_addr": "boss@co", "subject": "ok", "body": "go"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["step_runs"][0]["status"] == "completed"
    assert body["step_runs"][0]["output"]["email"]["subject"] == "ok"


def test_http_resolve_email_filter_mismatch_400(client):
    run_id = _park_via_service(client, from_filter="@safe.io")
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/email/wait",
        json={"from_addr": "stranger@evil.io", "subject": ""})
    assert r.status_code == 400


def test_http_resolve_email_unknown_run_404(client):
    r = client.post(
        "/tenants/tenant1/projects/p1/workflow-runs/ghost/email/wait",
        json={})
    assert r.status_code == 404


def test_http_resolve_email_accepts_from_alias(client):
    """Inbound-parse payloads often use 'from' instead of 'from_addr'."""
    run_id = _park_via_service(client)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/email/wait",
        json={"from": "alias@x", "subject": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["step_runs"][0]["output"]["email"]["from"] == "alias@x"


def test_http_resolve_email_no_auth_required(client):
    """External email routers don't carry a tenant bearer."""
    run_id = _park_via_service(client)
    r = client.post(
        f"/tenants/tenant1/projects/p1/workflow-runs/{run_id}/email/wait",
        json={"from_addr": "x@y", "subject": "z"})
    assert r.status_code == 200
