"""Email notification dispatcher module."""

from src.notifications.email.dispatcher import DispatcherSettings, EmailDispatcher
from src.notifications.email.transport import (
    DryRunTransport,
    MailTransport,
    MockMailTransport,
    SmtpMailTransport,
)

__all__ = [
    "DispatcherSettings",
    "DryRunTransport",
    "EmailDispatcher",
    "MailTransport",
    "MockMailTransport",
    "SmtpMailTransport",
]
