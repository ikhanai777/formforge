"""Sending mail, and a local adapter so the flow runs with no provider.

Password reset needs somewhere to send a message. That was the reason it did
not exist through Phase 1: there is no email provider, no sender domain and no
credential, and a reset endpoint that silently fails to deliver is worse than
none -- it looks like a working recovery path to the person who most needs one.

`OutboxMailer` is the answer to that. It writes each message to a file, so the
entire flow -- request a reset, receive a link, use it once, watch the old
sessions die -- runs and is tested end to end on a laptop with nothing
configured. It is not a stub: the same `Mailer` interface serves it and SMTP,
and swapping them is a setting.

**Nothing here logs a message body.** A reset mail contains a token that is, for
the next half hour, equivalent to the password. It goes to the outbox file or
to the SMTP socket and nowhere else -- not to a log line, not into an exception
message. `deliver()` returns the path or the message id, never the content.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from ..config import EmailMode, Settings

log = logging.getLogger("formforge.accounts.email")


class EmailError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    name: str

    def send(self, message: Message) -> str:
        """Deliver it. Returns an identifier for the delivery, never the body."""


class OutboxMailer:
    """Writes each message to a file instead of sending it.

    What makes the password-reset flow testable with no credential. A
    deployment must not run on this -- production refuses to start with reset
    enabled and email set to anything but SMTP -- but locally it is the whole
    feature, working, inspectable with `cat`.
    """

    name = "outbox"

    def __init__(self, directory: Path | str, sender: str = "formforge@localhost"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sender = sender

    def send(self, message: Message) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        # The recipient is in the filename so an operator can find one; the
        # body is only ever in the file.
        safe = "".join(c if c.isalnum() or c in ".-_@" else "_" for c in message.to)
        path = self.directory / f"{stamp}-{safe}.eml"
        mail = EmailMessage()
        mail["From"] = self.sender
        mail["To"] = message.to
        mail["Subject"] = message.subject
        mail["Date"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mail.set_content(message.body)
        path.write_bytes(bytes(mail))
        # Deliberately not logging the subject or body: a reset mail carries a
        # token that is the password for the next half hour.
        log.info("wrote a message to the outbox (%s)", path.name)
        return str(path)

    def read_all(self) -> list[str]:
        """Every message written, oldest first. For tests and for an operator
        checking what a local run actually produced."""
        return [p.read_text() for p in sorted(self.directory.glob("*.eml"))]


class SmtpMailer:
    """A real mail server.

    **Unverified.** There is no SMTP credential in the development
    environment, so this has been exercised against the interface and not
    against a server. It needs a smoke test before it carries a reset anyone
    depends on.
    """

    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        user: str = "",
        password: str = "",
        sender: str = "formforge@localhost",
        timeout: float = 10.0,
    ):
        if not host:
            raise EmailError("SMTP needs a host")
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.sender = sender
        self.timeout = timeout

    def send(self, message: Message) -> str:
        mail = EmailMessage()
        mail["From"] = self.sender
        mail["To"] = message.to
        mail["Subject"] = message.subject
        mail.set_content(message.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                server.starttls()
                if self.user:
                    server.login(self.user, self._password)
                server.send_message(mail)
        except Exception as exc:
            # The exception text can carry the envelope; the body it cannot,
            # because it is not in scope here. Re-raised without the message.
            raise EmailError(f"could not send mail: {type(exc).__name__}") from None
        return message.to


class NullMailer:
    """Accepts and discards.

    For a deployment that has switched password reset off. It exists so that
    "email is disabled" is a configuration rather than a None check at every
    call site.
    """

    name = "disabled"

    def send(self, message: Message) -> str:
        log.warning("email is disabled; a message to %s was discarded", message.to)
        return ""


def open_mailer(settings: Settings | None = None) -> Mailer:
    """The mailer this configuration asks for."""
    config = settings or Settings.from_env()
    if config.email is EmailMode.SMTP:
        return SmtpMailer(
            config.smtp_host,
            config.smtp_port,
            user=config.smtp_user,
            password=config.smtp_password.reveal(),
            sender=config.email_from,
        )
    if config.email is EmailMode.DISABLED:
        return NullMailer()
    return OutboxMailer(config.email_outbox, sender=config.email_from)
