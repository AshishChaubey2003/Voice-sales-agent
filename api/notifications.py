"""Email notification for human handoff. Disabled by default; never blocks or fails a handoff.

HANDOFF_EMAIL_ENABLED=false (default) -> NullNotifier: nothing is sent and the request is recorded
as notification_status="not_configured". When enabled, SMTP settings are required; SmtpNotifier sends one
plain-text mail to the available reps (or all reps when nobody is available) using the standard library.
A send failure is logged without the message body or credentials and recorded as "failed".

Not telephony: this only emails the sales team that a visitor is waiting.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from api.config import Settings, get_settings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class HandoffNotice:
    organization_name: str
    handoff_id: str
    conversation_id: str
    reason: str | None
    context_summary: str
    recipients: tuple[str, ...]


class Notifier(Protocol):
    async def notify(self, notice: HandoffNotice) -> str:
        """Returns "sent" or "not_configured". Raises on failure (the caller records "failed")."""


class NullNotifier:
    async def notify(self, notice: HandoffNotice) -> str:
        return "not_configured"


def build_message(settings: Settings, notice: HandoffNotice) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"[{notice.organization_name}] A visitor is asking for a human"
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(notice.recipients)
    reason = notice.reason or "No reason given"
    message.set_content(
        "A visitor asked to speak with the sales team.\n\n"
        f"Handoff: {notice.handoff_id}\nConversation: {notice.conversation_id}\nReason: {reason}\n\n"
        "Recent messages:\n"
        f"{notice.context_summary or '(none)'}\n\n"
        "Open the sales dashboard to accept the chat."
    )
    return message


class SmtpNotifier:
    def __init__(self, settings: Settings):
        self._settings = settings

    def _send_blocking(self, notice: HandoffNotice) -> None:
        s = self._settings
        message = build_message(s, notice)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as client:
            if s.smtp_use_starttls:
                client.starttls(context=ssl.create_default_context())
            if s.smtp_username and s.smtp_password:
                client.login(s.smtp_username, s.smtp_password.get_secret_value())
            client.send_message(message)

    async def notify(self, notice: HandoffNotice) -> str:
        if not notice.recipients:
            return "not_configured"
        await asyncio.to_thread(self._send_blocking, notice)
        return "sent"


def notifier_from_settings(settings: Settings) -> Notifier:
    if settings.handoff_email_enabled and settings.smtp_host and settings.smtp_from:
        return SmtpNotifier(settings)
    return NullNotifier()


def get_notifier() -> Notifier:
    """FastAPI dependency; tests override it with a fake."""
    return notifier_from_settings(get_settings())
