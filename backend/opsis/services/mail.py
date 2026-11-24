"""Email (FEATURE_EMAIL) over SMTP: invitations, password resets, the test email
and emailed dashboards.

A message is sent while the request or job that makes it runs, with a timeout,
and is never stored: a password link must not sit in a table. Each message has
a plain-text part and a simple HTML part with inline styles, which mail clients
show reliably.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from html import escape

from opsis.settings import Settings

log = logging.getLogger("opsis.mail")

TIMEOUT = 20.0
BUTTON = (
    "display:inline-block;padding:10px 16px;border-radius:6px;background:#2563eb;"
    "color:#ffffff;text-decoration:none;font-weight:600"
)
BODY = "font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;color:#18181b"


class MailError(Exception):
    """The message could not be sent. `message` is safe to show (no secrets)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Message:
    subject: str
    text: str
    html: str


def send(settings: Settings, to: str, message: Message) -> None:
    sender = settings.smtp_from
    email = EmailMessage()
    email["From"] = sender
    email["To"] = to
    email["Subject"] = message.subject
    email["Date"] = formatdate(localtime=True)
    domain = parseaddr(sender)[1].partition("@")[2] or None
    email["Message-ID"] = make_msgid(domain=domain)
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")
    context = ssl.create_default_context()
    try:
        server: smtplib.SMTP
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=TIMEOUT, context=context
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
        with server:
            if settings.smtp_security == "starttls":
                server.starttls(context=context)
            if settings.smtp_username:
                password = (
                    settings.smtp_password.get_secret_value() if settings.smtp_password else ""
                )
                server.login(settings.smtp_username, password)
            server.send_message(email)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server did not accept SMTP_USERNAME and SMTP_PASSWORD.") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(
            f"The email to {to} could not be sent through {settings.smtp_host}: "
            f"{type(exc).__name__}: {exc}"[:400]
        ) from None
    log.info("Email sent to %s: %s", to, message.subject)


def page(settings: Settings, parts: list[str]) -> str:
    """Already escaped HTML parts in the message body, with the app's name below."""
    return (
        f'<div style="{BODY}">'
        + "".join(parts)
        + f'<p style="margin:24px 0 0;color:#71717a;font-size:13px">{escape(settings.app_name)}, '
        f"{escape(settings.public_base_url)}</p></div>"
    )


def _page(settings: Settings, paragraphs: list[str], button: tuple[str, str] | None) -> str:
    parts = [f'<p style="margin:0 0 14px">{p}</p>' for p in paragraphs]
    if button:
        label, url = button
        parts.append(
            f'<p style="margin:20px 0"><a href="{escape(url, quote=True)}" style="{BUTTON}">'
            f"{escape(label)}</a></p>"
        )
    return page(settings, parts)


def invitation(settings: Settings, name: str, link: str, invited_by: str) -> Message:
    app = settings.app_name
    lines = [
        f"Hello {name},",
        f"{invited_by} has invited you to {app}. Choose a password to start; the link works "
        "once and for 7 days.",
    ]
    return Message(
        subject=f"You are invited to {app}",
        text="\n\n".join([*lines, link, f"{app}, {settings.public_base_url}"]) + "\n",
        html=_page(settings, [escape(line) for line in lines], ("Choose a password", link)),
    )


def password_reset(settings: Settings, name: str, link: str) -> Message:
    app = settings.app_name
    lines = [
        f"Hello {name},",
        f"Use this link to choose a new password for {app}. It works once and for 24 hours.",
        "If you did not ask for this, you can ignore this email: your password stays as it is.",
    ]
    return Message(
        subject=f"Set a new password for {app}",
        text="\n\n".join([lines[0], lines[1], link, lines[2], f"{app}, {settings.public_base_url}"])
        + "\n",
        html=_page(settings, [escape(line) for line in lines], ("Choose a new password", link)),
    )


def test_message(settings: Settings) -> Message:
    line = f"This is a test email from {settings.app_name}. Email works."
    return Message(
        subject=f"Test email from {settings.app_name}",
        text=f"{line}\n",
        html=_page(settings, [escape(line)], None),
    )
