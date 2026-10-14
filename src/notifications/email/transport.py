"""Mail transport abstraction and implementations.

This module owns both halves of the email path: assembling the RFC 5322 /
MIME payload that is handed to ``smtplib.SMTP.sendmail``, and performing the
SMTP conversation itself. ``MockMailTransport`` records the same arguments in
memory so the alerting flow can be exercised without a live mail server.

The message is assembled from the standard library only (``base64``,
``mimetypes``, ``uuid``, ``datetime``), so the transport carries no project
dependency of its own and can be imported from any layer of the service.

This package directory is itself named ``email``. Any ``email.*`` module-level
import would therefore resolve against this package rather than against the
standard library, which makes the module unimportable. ``smtplib`` -- which
itself pulls in ``email.mime.*`` at import time -- is consequently imported
lazily inside :meth:`SmtpMailTransport.send`, the single place that opens a
live connection, so importing this module never touches the ``email`` name.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Union

logger = logging.getLogger(__name__)

__all__ = [
    "AttachmentContent",
    "DryRunTransport",
    "MailTransport",
    "MockMailTransport",
    "SmtpMailTransport",
    "build_mime_message",
]

# Binary payload carried by a single attachment entry.
AttachmentContent = Union[bytes, bytearray, str]

_MIME_DATE_FORMAT = "%a, %d %b %Y %H:%M:%S +0000"
_MESSAGE_ID_DOMAIN = "ventura-coa.local"
_BASE64_LINE_WIDTH = 76
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _encode_header_value(value: str) -> str:
    """Return ``value`` as-is when ASCII, else as an RFC 2047 base64 word."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return "=?UTF-8?B?" + encoded + "?="
    return value


def _wrap_base64(payload: bytes) -> str:
    """Base64-encode ``payload`` and fold it into CRLF-terminated lines."""
    encoded = base64.b64encode(payload).decode("ascii")
    width = _BASE64_LINE_WIDTH
    return "\r\n".join(
        encoded[offset : offset + width]
        for offset in range(0, len(encoded), width)
    )


def _as_bytes(content: Optional[AttachmentContent]) -> bytes:
    """Normalise an attachment payload to bytes."""
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if content is None:
        return b""
    return str(content).encode("utf-8")


def _safe_filename(name: str) -> str:
    """Strip characters that would break a quoted MIME header parameter."""
    cleaned = name.replace('"', "").replace("\r", "").replace("\n", "")
    return cleaned.strip() or "attachment"


def _percent_encode(value: str) -> str:
    """Percent-encode ``value`` for an RFC 6266 ``filename*`` parameter."""
    chars: List[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        chars.append(char if char in _UNRESERVED else f"%{byte:02X}")
    return "".join(chars)


def _text_part(content_type: str, text: str) -> str:
    """Build one base64-encoded body part, newline terminated."""
    headers = [
        f"Content-Type: {content_type}",
        "Content-Transfer-Encoding: base64",
    ]
    body = _wrap_base64(text.encode("utf-8"))
    return "\r\n".join(headers) + "\r\n\r\n" + body + "\r\n"


def _alternative_block(
    body_text: str,
    body_html: str,
    prefix: str = "",
) -> str:
    """Build a ``multipart/alternative`` part (text, then HTML when given)."""
    boundary = uuid.uuid4().hex
    chunks: List[str] = [
        prefix,
        f'Content-Type: multipart/alternative; boundary="{boundary}"',
        "\r\n\r\n",
        f"--{boundary}\r\n",
        _text_part("text/plain; charset=UTF-8", body_text),
    ]
    if body_html:
        chunks.append(f"--{boundary}\r\n")
        chunks.append(_text_part("text/html; charset=UTF-8", body_html))
    chunks.append(f"--{boundary}--\r\n")
    return "".join(chunks)


def _attachment_block(attachment: Dict[str, AttachmentContent]) -> str:
    """Build one ``base64`` attachment part for a filename/content mapping."""
    filename = _safe_filename(str(attachment.get("filename") or "attachment"))
    payload = _as_bytes(attachment.get("content"))
    guessed_type, _encoding = mimetypes.guess_type(filename)
    media_type = guessed_type or "application/octet-stream"
    maintype, _slash, subtype = media_type.partition("/")
    disposition = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{_percent_encode(filename)}"
    )
    headers = [
        f'Content-Type: {maintype}/{subtype}; name="{filename}"',
        "Content-Transfer-Encoding: base64",
        f"Content-Disposition: {disposition}",
    ]
    return "\r\n".join(headers) + "\r\n\r\n" + _wrap_base64(payload) + "\r\n"


def build_mime_message(
    sender: str,
    recipients: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Optional[List[Dict[str, AttachmentContent]]] = None,
) -> str:
    """Assemble the complete raw message: headers, blank line, then body."""
    now = datetime.now(timezone.utc)
    attachment_entries = list(attachments or [])
    headers = [
        f"Date: {now.strftime(_MIME_DATE_FORMAT)}",
        f"From: {_encode_header_value(sender)}",
        "To: "
        + ", ".join(_encode_header_value(r) for r in recipients),
        f"Subject: {_encode_header_value(subject)}",
        f"Message-ID: <{uuid.uuid4().hex}@{_MESSAGE_ID_DOMAIN}>",
        "MIME-Version: 1.0",
    ]

    parts: List[str] = []
    if attachment_entries:
        boundary = uuid.uuid4().hex
        headers.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')
        parts.append(
            _alternative_block(
                body_text, body_html or "", prefix=f"--{boundary}\r\n"
            )
        )
        for attachment in attachment_entries:
            parts.append(f"--{boundary}\r\n" + _attachment_block(attachment))
        closing = f"--{boundary}--\r\n"
    elif body_html:
        boundary = uuid.uuid4().hex
        headers.append(
            f'Content-Type: multipart/alternative; boundary="{boundary}"'
        )
        parts.append(_alternative_block(body_text, body_html))
        closing = f"--{boundary}--\r\n"
    else:
        parts.append(_text_part("text/plain; charset=UTF-8", body_text))
        closing = ""

    return "\r\n".join(headers) + "\r\n\r\n" + "".join(parts) + closing


class MailTransport(Protocol):
    """Protocol for mail transport implementations."""

    def send(
        self,
        sender: str,
        recipients: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        attachments: Optional[List[Dict[str, AttachmentContent]]] = None,
    ) -> bool:
        """Send an email message.

        Args:
            sender: From address.
            recipients: List of To addresses.
            subject: Email subject line.
            body_text: Plain text body.
            body_html: Optional HTML body.
            attachments: Optional list of dicts with 'filename' and 'content'.

        Returns:
            True if send succeeded, False otherwise.
        """
        ...


class SmtpMailTransport:
    """SMTP-based mail transport implementation."""

    def __init__(
        self,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout

    def send(
        self,
        sender: str,
        recipients: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        attachments: Optional[List[Dict[str, AttachmentContent]]] = None,
    ) -> bool:
        """Send email via SMTP."""
        # Imported here, not at module scope: ``smtplib`` pulls in the
        # standard library ``email`` package at import time, and inside this
        # package the name ``email`` refers to this directory.
        import smtplib

        raw_message = build_mime_message(
            sender=sender,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )
        try:
            with smtplib.SMTP(
                self._host, self._port, timeout=self._timeout
            ) as server:
                if self._use_tls:
                    server.starttls()
                if self._username and self._password:
                    server.login(self._username, self._password)
                server.sendmail(sender, recipients, raw_message)
            logger.info(
                "Email sent successfully",
                extra={"recipients": recipients, "subject": subject},
            )
            return True
        except smtplib.SMTPException:
            logger.exception(
                "SMTP error sending email",
                extra={"recipients": recipients, "subject": subject},
            )
            return False
        except OSError:
            logger.exception(
                "Network error sending email",
                extra={"recipients": recipients, "subject": subject},
            )
            return False


class DryRunTransport:
    """Assembles every message for real and records it instead of sending it.

    This is the transport a deployment runs when no SMTP host is configured:
    the dispatcher, its channel guard, its retry policy and its idempotency keys
    all execute normally and the MIME payload is built exactly as it would be
    for the wire, so the alerting pipeline is exercised end to end without a mail
    server and without a message escaping the process.

    It is an operational mode, not a test double - which is the difference from
    :class:`MockMailTransport`: that one only records the arguments it was handed
    (and can be told to fail, so a test can drive the retry path), while this one
    renders the message and reports success, exactly as a delivered send would.
    """

    def __init__(self) -> None:
        self.outbox: List[Dict[str, object]] = []

    def send(
        self,
        sender: str,
        recipients: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        attachments: Optional[List[Dict[str, AttachmentContent]]] = None,
    ) -> bool:
        """Render the message into the outbox and report it as sent."""
        raw_message = build_mime_message(
            sender=sender,
            recipients=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )
        self.outbox.append(
            {
                "sender": sender,
                "recipients": list(recipients),
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "attachments": attachments or [],
                "raw_message": raw_message,
            }
        )
        logger.info(
            "DRY RUN: message rendered, not sent",
            extra={"recipients": list(recipients), "subject": subject},
        )
        return True


class MockMailTransport:
    """In-memory mock transport for testing without live infrastructure."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, object]] = []
        self._should_fail = False

    def set_should_fail(self, should_fail: bool) -> None:
        """Configure whether sends should fail."""
        self._should_fail = should_fail

    def send(
        self,
        sender: str,
        recipients: List[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        attachments: Optional[List[Dict[str, AttachmentContent]]] = None,
    ) -> bool:
        """Record the message without actually sending."""
        if self._should_fail:
            logger.warning("MockMailTransport configured to fail")
            return False

        self.sent_messages.append(
            {
                "sender": sender,
                "recipients": recipients,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "attachments": attachments or [],
            }
        )
        logger.debug(
            "Mock email recorded",
            extra={"recipients": recipients, "subject": subject},
        )
        return True
