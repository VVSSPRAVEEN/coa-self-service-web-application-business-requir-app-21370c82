"""Email-only notification dispatcher with channel guard, retry, and idempotency."""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from dataclasses import dataclass

from src.notifications.email.transport import MailTransport

logger = logging.getLogger(__name__)

ALLOWED_CHANNELS: frozenset[str] = frozenset({"email"})
BLOCKED_CHANNELS: frozenset[str] = frozenset({"slack", "webhook", "sms", "push"})


@dataclass(frozen=True)
class DispatcherSettings:
    """Configuration for the email dispatcher."""

    sender_address: str
    cs_recipients: tuple[str, ...]
    qa_recipients: tuple[str, ...]
    dry_run: bool = False
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    smtp_mode: str = "live"


class ChannelGuardError(Exception):
    """Raised when a disallowed notification channel is requested."""


class IdempotencyStore:
    """In-memory store for tracking dispatched idempotency keys."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def has_been_sent(self, key: str) -> bool:
        """Check if this key was already dispatched."""
        return key in self._seen

    def mark_sent(self, key: str) -> None:
        """Record that this key has been dispatched."""
        self._seen.add(key)

    def clear(self) -> None:
        """Reset the store (for testing)."""
        self._seen.clear()


class EmailDispatcher:
    """Dispatches email-only notifications with retry, idempotency, and channel guard."""

    def __init__(
        self,
        transport: MailTransport,
        settings: DispatcherSettings,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._idempotency = idempotency_store or IdempotencyStore()

    def enforce_channel(self, channel: str) -> None:
        """Reject any channel not in the email allow-list.

        Args:
            channel: The requested notification channel.

        Raises:
            ChannelGuardError: If the channel is not 'email'.
        """
        normalized = channel.strip().lower()
        if normalized in BLOCKED_CHANNELS:
            logger.error(
                "Blocked notification channel attempted",
                extra={"channel": normalized, "allowed": list(ALLOWED_CHANNELS)},
            )
            raise ChannelGuardError(
                f"Channel '{normalized}' is not allowed. Only {list(ALLOWED_CHANNELS)} are permitted."
            )
        if normalized not in ALLOWED_CHANNELS:
            logger.error(
                "Unknown notification channel rejected",
                extra={"channel": normalized},
            )
            raise ChannelGuardError(
                f"Channel '{normalized}' is not in the allow-list: {list(ALLOWED_CHANNELS)}"
            )

    def generate_idempotency_key(self, report_window: str, recipient: str) -> str:
        """Generate a deterministic idempotency key for a report window + recipient pair.

        Args:
            report_window: The reporting period identifier.
            recipient: The target email address.

        Returns:
            A SHA-256 hex digest string.
        """
        raw = f"{report_window}:{recipient}:{self._settings.sender_address}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def resolve_recipients(self, distribution_lists: list[str] | None = None) -> list[str]:
        """Resolve unique recipients from CS and QA distribution lists.

        Args:
            distribution_lists: Optional filter; if provided, only include matching lists.

        Returns:
            Deduplicated list of email addresses.
        """
        recipients: set[str] = set()
        include_cs = distribution_lists is None or "cs" in distribution_lists
        include_qa = distribution_lists is None or "qa" in distribution_lists

        if include_cs:
            recipients.update(self._settings.cs_recipients)
        if include_qa:
            recipients.update(self._settings.qa_recipients)

        return sorted(recipients)

    def dispatch(
        self,
        report_window: str,
        subject: str,
        body_text: str,
        csv_content: str,
        distribution_lists: list[str] | None = None,
        channel: str = "email",
        body_html: str | None = None,
    ) -> bool:
        """Dispatch the report email to all resolved recipients.

        Enforces channel guard, checks idempotency, performs per-recipient retry
        with bounded exponential backoff, and supports dry-run mode.

        Args:
            report_window: Identifier for the reporting window (idempotency scope).
            subject: Email subject line.
            body_text: Plain text email body (inline table).
            csv_content: CSV attachment content.
            distribution_lists: Which lists to send to ('cs', 'qa', or both).
            channel: Must be 'email'; others are rejected.
            body_html: Optional HTML version of the body.

        Returns:
            True if all recipients succeeded or dry_run mode.

        Raises:
            ChannelGuardError: If channel is not 'email'.
        """
        self.enforce_channel(channel)

        recipients = self.resolve_recipients(distribution_lists)
        if not recipients:
            logger.warning("No recipients resolved for dispatch")
            return True

        attachments: list[dict[str, bytes | str]] = [
            {
                "filename": f"missing-coa-report-{report_window}.csv",
                "content": csv_content.encode("utf-8"),
            }
        ]

        all_succeeded = True
        results: dict[str, str] = {}

        for recipient in recipients:
            idem_key = self.generate_idempotency_key(report_window, recipient)

            if self._idempotency.has_been_sent(idem_key):
                logger.info(
                    "Skipping duplicate dispatch",
                    extra={"recipient": recipient, "idempotency_key": idem_key},
                )
                results[recipient] = "skipped_idempotent"
                continue

            if self._settings.dry_run:
                logger.info(
                    "DRY RUN: Would send email",
                    extra={
                        "recipient": recipient,
                        "subject": subject,
                        "report_window": report_window,
                    },
                )
                self._idempotency.mark_sent(idem_key)
                results[recipient] = "dry_run"
                continue

            success = self._send_with_retry(
                recipient=recipient,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                attachments=attachments,
                idem_key=idem_key,
            )

            if success:
                results[recipient] = "sent"
            else:
                results[recipient] = "permanently_failed"
                all_succeeded = False

        logger.info(
            "Dispatch complete",
            extra={
                "report_window": report_window,
                "results": results,
                "all_succeeded": all_succeeded,
            },
        )

        if not all_succeeded:
            failed = [r for r, status in results.items() if status == "permanently_failed"]
            logger.error(
                "One or more recipients permanently failed",
                extra={"failed_recipients": failed},
            )

        return all_succeeded

    def _send_with_retry(
        self,
        recipient: str,
        subject: str,
        body_text: str,
        body_html: str | None,
        attachments: list[dict[str, bytes | str]],
        idem_key: str,
    ) -> bool:
        """Attempt to send with bounded exponential backoff retry.

        Args:
            recipient: Target email address.
            subject: Email subject.
            body_text: Plain text body.
            body_html: Optional HTML body.
            attachments: File attachments.
            idem_key: Idempotency key to mark on success.

        Returns:
            True if eventually successful, False after exhausting retries.
        """
        delay = self._settings.retry_base_delay
        last_error: str | None = None

        for attempt in range(1, self._settings.max_retries + 1):
            logger.info(
                "Sending email attempt",
                extra={
                    "recipient": recipient,
                    "attempt": attempt,
                    "max_retries": self._settings.max_retries,
                },
            )

            success = self._transport.send(
                sender=self._settings.sender_address,
                recipients=[recipient],
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                attachments=attachments,
            )

            if success:
                self._idempotency.mark_sent(idem_key)
                logger.info(
                    "Email sent successfully",
                    extra={"recipient": recipient, "attempt": attempt},
                )
                return True

            last_error = f"Attempt {attempt} failed for {recipient}"
            logger.warning(last_error)

            if attempt < self._settings.max_retries:
                sleep_time = min(delay, self._settings.retry_max_delay)
                logger.debug(
                    "Backing off before retry",
                    extra={"delay_seconds": sleep_time, "recipient": recipient},
                )
                time.sleep(sleep_time)
                delay *= 2

        logger.error(
            "Permanently failed to send email after all retries",
            extra={
                "recipient": recipient,
                "attempts": self._settings.max_retries,
                "last_error": last_error,
            },
        )
        return False

    def dispatch_or_exit(
        self,
        report_window: str,
        subject: str,
        body_text: str,
        csv_content: str,
        distribution_lists: list[str] | None = None,
        channel: str = "email",
        body_html: str | None = None,
    ) -> None:
        """Dispatch and exit with non-zero code if any recipient permanently fails.

        This is the CLI entry point wrapper.
        """
        try:
            success = self.dispatch(
                report_window=report_window,
                subject=subject,
                body_text=body_text,
                csv_content=csv_content,
                distribution_lists=distribution_lists,
                channel=channel,
                body_html=body_html,
            )
        except ChannelGuardError as exc:
            logger.critical("Channel guard violation: %s", exc)
            sys.exit(1)

        if not success:
            logger.critical("Dispatch completed with permanent failures; exiting non-zero")
            sys.exit(1)

        logger.info("Dispatch completed successfully")