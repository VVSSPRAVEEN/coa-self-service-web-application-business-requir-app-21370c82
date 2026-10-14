"""Retry decorator/wrapper with exponential backoff and DLQ integration."""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable, TypeVar, overload

from src.integration.retry.policy import RetryPolicy, DEFAULT_RETRY_POLICY
from src.integration.retry.errors import classify_error
from src.integration.retry.dlq import DeadLetterQueue

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Global metrics for retry operations
RETRY_METRICS: dict[str, int] = {
    "retry_attempts_total": 0,
    "retry_successes": 0,
    "retry_exhaustions": 0,
    "retry_unrecoverable": 0,
}


@overload
def retry_call(func: F) -> F: ...


@overload
def retry_call(
    func: None = None,
    *,
    policy: RetryPolicy | None = None,
    dlq: DeadLetterQueue | None = None,
    record_id_fn: Callable[..., str] | None = None,
) -> Callable[[F], F]: ...


def retry_call(
    func: F | None = None,
    *,
    policy: RetryPolicy | None = None,
    dlq: DeadLetterQueue | None = None,
    record_id_fn: Callable[..., str] | None = None,
) -> F | Callable[[F], F]:
    """Decorator/wrapper that retries a function with exponential backoff.

    Classifies errors as transient vs. unrecoverable. Transient errors
    trigger retries according to the policy. Unrecoverable errors and
    exhausted retries are recorded in the Dead Letter Queue.

    Can be used as a bare decorator (@retry_call) or with arguments
    (@retry_call(policy=..., dlq=...)).

    Args:
        func: The function to wrap (when used as bare decorator).
        policy: Retry policy configuration. Defaults to DEFAULT_RETRY_POLICY.
        dlq: Dead letter queue instance for recording failures.
        record_id_fn: Optional callable to extract a record ID from function args
            for DLQ idempotency. If None, a hash of args is used.

    Returns:
        The wrapped function with retry behavior.
    """
    effective_policy = policy or DEFAULT_RETRY_POLICY

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Determine record ID for DLQ tracking
            if record_id_fn is not None:
                try:
                    rec_id = record_id_fn(*args, **kwargs)
                except Exception:
                    rec_id = f"{fn.__name__}_{id(args)}_{time.time()}"
            else:
                rec_id = f"{fn.__name__}_{hash(str(args) + str(sorted(kwargs.items())))}"

            attempt = 0

            while True:
                attempt += 1
                RETRY_METRICS["retry_attempts_total"] += 1

                try:
                    result = fn(*args, **kwargs)
                    RETRY_METRICS["retry_successes"] += 1
                    logger.debug(
                        "retry_success",
                        extra={
                            "function": fn.__name__,
                            "record_id": rec_id,
                            "attempt": attempt,
                            "event": "retry_success",
                        },
                    )
                    return result

                except Exception as exc:
                    error_class = classify_error(exc)

                    logger.warning(
                        "retry_attempt_failed",
                        extra={
                            "function": fn.__name__,
                            "record_id": rec_id,
                            "attempt": attempt,
                            "max_attempts": effective_policy.max_attempts,
                            "error_type": type(exc).__name__,
                            "error_class": error_class,
                            "error_message": str(exc),
                            "event": "retry_failure",
                        },
                    )

                    # Unrecoverable errors go straight to DLQ
                    if error_class == "unrecoverable":
                        RETRY_METRICS["retry_unrecoverable"] += 1
                        logger.error(
                            "retry_unrecoverable_error",
                            extra={
                                "function": fn.__name__,
                                "record_id": rec_id,
                                "attempt": attempt,
                                "error_type": type(exc).__name__,
                                "event": "retry_unrecoverable",
                            },
                        )
                        if dlq is not None:
                            dlq.write(
                                record_id=rec_id,
                                payload={"args": str(args), "kwargs": str(kwargs)},
                                attempt_count=attempt,
                                error=exc,
                                source=fn.__name__,
                            )
                        raise

                    # Check if we have attempts remaining
                    if not effective_policy.should_retry(attempt):
                        RETRY_METRICS["retry_exhaustions"] += 1
                        logger.error(
                            "retry_exhausted",
                            extra={
                                "function": fn.__name__,
                                "record_id": rec_id,
                                "attempt": attempt,
                                "max_attempts": effective_policy.max_attempts,
                                "error_type": type(exc).__name__,
                                "event": "retry_exhausted",
                                "metric_retry_exhaustions": RETRY_METRICS["retry_exhaustions"],
                            },
                        )
                        if dlq is not None:
                            dlq.write(
                                record_id=rec_id,
                                payload={"args": str(args), "kwargs": str(kwargs)},
                                attempt_count=attempt,
                                error=exc,
                                source=fn.__name__,
                            )
                        raise

                    # Calculate delay and sleep
                    delay = effective_policy.get_delay(attempt - 1)
                    logger.info(
                        "retry_backoff",
                        extra={
                            "function": fn.__name__,
                            "record_id": rec_id,
                            "attempt": attempt,
                            "delay_seconds": round(delay, 3),
                            "event": "retry_backoff",
                        },
                    )
                    time.sleep(delay)

        return wrapper  # type: ignore[return-value]

    # Handle both @retry_call and @retry_call(...) usage
    if func is not None:
        return decorator(func)
    return decorator


def execute_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    policy: RetryPolicy | None = None,
    dlq: DeadLetterQueue | None = None,
    record_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Execute a callable with retry logic without using it as a decorator.

    This is a convenience wrapper for one-off calls.

    Args:
        fn: The callable to execute.
        *args: Positional arguments for the callable.
        policy: Retry policy configuration.
        dlq: Dead letter queue instance.
        record_id: Optional explicit record ID for DLQ tracking.
        **kwargs: Keyword arguments for the callable.

    Returns:
        The result of the callable.
    """
    effective_policy = policy or DEFAULT_RETRY_POLICY
    rec_id = record_id or f"call_{fn.__name__}_{time.time()}"

    attempt = 0

    while True:
        attempt += 1
        RETRY_METRICS["retry_attempts_total"] += 1

        try:
            result = fn(*args, **kwargs)
            RETRY_METRICS["retry_successes"] += 1
            return result
        except Exception as exc:
            error_class = classify_error(exc)

            if error_class == "unrecoverable":
                RETRY_METRICS["retry_unrecoverable"] += 1
                if dlq is not None:
                    dlq.write(
                        record_id=rec_id,
                        payload={"args": str(args), "kwargs": str(kwargs)},
                        attempt_count=attempt,
                        error=exc,
                        source=fn.__name__,
                    )
                raise

            if not effective_policy.should_retry(attempt):
                RETRY_METRICS["retry_exhaustions"] += 1
                if dlq is not None:
                    dlq.write(
                        record_id=rec_id,
                        payload={"args": str(args), "kwargs": str(kwargs)},
                        attempt_count=attempt,
                        error=exc,
                        source=fn.__name__,
                    )
                raise

            delay = effective_policy.get_delay(attempt - 1)
            logger.info(
                "retry_backoff",
                extra={
                    "function": fn.__name__,
                    "record_id": rec_id,
                    "attempt": attempt,
                    "delay_seconds": round(delay, 3),
                    "event": "retry_backoff",
                },
            )
            time.sleep(delay)
