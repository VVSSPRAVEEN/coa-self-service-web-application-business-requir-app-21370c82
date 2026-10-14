"""Error classification for retry logic."""

from __future__ import annotations


class TransientError(Exception):
    """An error that may succeed on retry (e.g., network timeout, 503)."""

    pass


class UnrecoverableError(Exception):
    """An error that will never succeed on retry (e.g., validation failure, 400)."""

    pass


def classify_error(exc: BaseException) -> str:
    """Classify an exception as transient or unrecoverable.

    Args:
        exc: The exception to classify.

    Returns:
        'transient' if the error is retryable, 'unrecoverable' otherwise.
    """
    if isinstance(exc, UnrecoverableError):
        return "unrecoverable"
    if isinstance(exc, TransientError):
        return "transient"

    # Default classification based on common exception types
    transient_types = (
        ConnectionError,
        TimeoutError,
        OSError,
    )

    if isinstance(exc, transient_types):
        return "transient"

    # Check for HTTP-like status codes if available
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if 500 <= int(status_code) < 600 or int(status_code) == 429:
            return "transient"
        if 400 <= int(status_code) < 500:
            return "unrecoverable"

    # Unknown errors are treated as transient to avoid silent data loss
    return "transient"
