"""W4 — Email subsystem for workflow email_wait steps.

Two halves:

- **Outbound (SMTP):** EmailService.send(message) sends via a
  configured SMTP server. Used by action steps that need to email
  out as part of the workflow (template lives in step.config).

- **Inbound (IMAP poll):** EmailService.poll_once() connects to a
  configured IMAP mailbox, drains unseen messages, and returns them
  as plain dicts. The caller (a workflow scheduler / external
  forwarder) is responsible for matching them to parked runs and
  invoking ``WorkflowService.resolve_email_wait``.

We deliberately do NOT run an SMTP/IMAP server in-process. Operators
wire EmailService up to their existing mail infra. The HTTP route
in ``server/routes/workflow_v2.py`` accepts posted emails for the
common "email router → API call" integration (SendGrid Inbound Parse,
Postmark, CloudMailin, etc.).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Iterable

log = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class SMTPConfig:
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    from_addr: str | None = None
    timeout: float = 30.0


@dataclass
class IMAPConfig:
    host: str
    username: str
    password: str
    port: int = 993
    use_ssl: bool = True
    mailbox: str = "INBOX"
    timeout: float = 30.0


@dataclass
class InboundEmail:
    """Normalized representation of one received message."""
    from_addr: str
    to_addr: str
    subject: str
    body: str
    raw_headers: dict = field(default_factory=dict)


# ── Service ────────────────────────────────────────────────────────────────

class EmailService:
    """SMTP + IMAP wrapper used by workflow email steps.

    The class is intentionally thin — it doesn't background-poll.
    Operators call ``poll_once`` on their own schedule (cron, k8s
    CronJob, systemd timer) and feed each message to
    ``WorkflowService.resolve_email_wait``.
    """

    def __init__(self, *, smtp: SMTPConfig | None = None,
                 imap: IMAPConfig | None = None):
        self.smtp = smtp
        self.imap = imap

    # ── Outbound ─────────────────────────────────────────────────────────
    def send(self, *, to: str, subject: str, body: str,
              from_addr: str | None = None,
              smtp_factory=None) -> str:
        """Send an email. Returns the from_addr used.

        ``smtp_factory`` is injectable for testing — production callers
        leave it None and we build a real smtplib.SMTP / SMTP_SSL
        connection from self.smtp.
        """
        if self.smtp is None and smtp_factory is None:
            raise RuntimeError("SMTP not configured")
        cfg = self.smtp or SMTPConfig(host="", port=0)
        sender = from_addr or cfg.from_addr or cfg.username
        if not sender:
            raise ValueError("from_addr must be set (param or config.from_addr)")

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        factory = smtp_factory or _default_smtp_factory(cfg)
        with factory() as smtp:
            if cfg.username and cfg.password:
                smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
        return sender

    # ── Inbound ──────────────────────────────────────────────────────────
    def poll_once(self, *, imap_factory=None) -> list[InboundEmail]:
        """Drain currently-unseen messages from the configured mailbox.

        Returns one InboundEmail per message. Marks them as seen.

        ``imap_factory`` is injectable for testing.
        """
        if self.imap is None and imap_factory is None:
            raise RuntimeError("IMAP not configured")
        factory = imap_factory or _default_imap_factory(self.imap)
        out: list[InboundEmail] = []
        with factory() as imap:
            imap.login(self.imap.username, self.imap.password)
            imap.select(self.imap.mailbox or "INBOX")
            typ, data = imap.search(None, "UNSEEN")
            if typ != "OK":
                return out
            for num in data[0].split():
                typ, msg_data = imap.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    parsed = _parse_raw_email(raw)
                    if parsed is not None:
                        out.append(parsed)
                # Mark as seen so we don't re-fetch next poll.
                imap.store(num, "+FLAGS", "\\Seen")
        return out


# ── Factories ──────────────────────────────────────────────────────────────

def _default_smtp_factory(cfg: SMTPConfig):
    def _open():
        if cfg.use_tls:
            ctx = ssl.create_default_context()
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
            smtp.starttls(context=ctx)
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        return smtp
    return _open


def _default_imap_factory(cfg: IMAPConfig | None):
    import imaplib
    def _open():
        if cfg is None:
            raise RuntimeError("IMAP not configured")
        if cfg.use_ssl:
            ctx = ssl.create_default_context()
            return imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=ctx)
        return imaplib.IMAP4(cfg.host, cfg.port)
    return _open


def _parse_raw_email(raw: bytes) -> InboundEmail | None:
    """Best-effort bytes → InboundEmail. Uses the stdlib email parser."""
    import email
    from email import policy
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
        from_addr = str(msg.get("From", ""))
        to_addr = str(msg.get("To", ""))
        subject = str(msg.get("Subject", ""))
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_content()
                    break
        else:
            body = msg.get_content()
        headers = {k: str(v) for k, v in msg.items()}
        return InboundEmail(from_addr=from_addr, to_addr=to_addr,
                            subject=subject, body=body,
                            raw_headers=headers)
    except Exception as e:
        log.warning("failed to parse inbound email: %s", e)
        return None


# ── Filter matching ────────────────────────────────────────────────────────

def matches_filters(email: InboundEmail, *,
                     from_filter: str | None = None,
                     subject_filter: str | None = None) -> bool:
    """True when the email passes both filters (None = no constraint).

    Substring match — case-insensitive — so operators can match a
    domain (``@example.com``) or a subject prefix without writing
    regex. Strict equality is rarely what operators want from email.
    """
    if from_filter and from_filter.lower() not in email.from_addr.lower():
        return False
    if subject_filter and subject_filter.lower() not in email.subject.lower():
        return False
    return True
