"""Email notification dispatcher module."""

from src.notifications.email.transport import (
    DryRunTransport,
    MailTransport,
    MockMailTransport,
    SmtpMailTransport,
)
from src.notifications.email.dispatcher import EmailDispatcher, DispatcherSettings

__all__ = [
    "DispatcherSettings",
    "DryRunTransport",
    "EmailDispatcher",
    "MailTransport",
    "MockMailTransport",
    "SmtpMailTransport",
]
