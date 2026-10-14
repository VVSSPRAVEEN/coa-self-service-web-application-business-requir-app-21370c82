"""DLQ re-drive entry point for replaying dead-lettered records."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.integration.retry.dlq import DeadLetterQueue

logger = logging.getLogger(__name__)


def redrive_dlq(
    dlq: DeadLetterQueue,
    handler: Callable[[Any], Any],
    *,
    batch_size: int = 100,
    stop_on_error: bool = False,
) -> dict[str, int]:
    """Replay dead-lettered records back into ingestion exactly once.

    Preserves idempotency keys so that previously processed records
    are not re-ingested.

    Args:
        dlq: The DeadLetterQueue instance containing records to replay.
        handler: A callable that processes each record's payload.
        batch_size: Maximum number of records to process per invocation.
        stop_on_error: If True, halt processing on first handler failure.

    Returns:
        A summary dict with counts of succeeded, failed, and skipped records.
    """
    unredriven = dlq.get_unredriven_records()
    results = {"succeeded": 0, "failed": 0, "skipped": 0, "total": len(unredriven)}

    records_to_process = unredriven[:batch_size]

    logger.info(
        "dlq_redrive_started",
        extra={
            "total_unredriven": len(unredriven),
            "batch_size": batch_size,
            "processing_count": len(records_to_process),
            "event": "dlq_redrive_start",
        },
    )

    for record in records_to_process:
        # Idempotency check: skip if already redriven
        if dlq.is_processed(f"redriven:{record.record_id}"):
            results["skipped"] += 1
            logger.debug(
                "dlq_redrive_skipped",
                extra={
                    "record_id": record.record_id,
                    "reason": "already_redriven",
                    "event": "dlq_redrive_skip",
                },
            )
            continue

        try:
            handler(record.payload)
            dlq.mark_redriven(record.record_id)
            results["succeeded"] += 1
            logger.info(
                "dlq_redrive_success",
                extra={
                    "record_id": record.record_id,
                    "original_attempts": record.attempt_count,
                    "error_type": record.error_type,
                    "event": "dlq_redrive_success",
                },
            )
        except Exception as exc:  # noqa: BLE001 - per-record redrive isolation
            results["failed"] += 1
            logger.error(
                "dlq_redrive_failed",
                extra={
                    "record_id": record.record_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "event": "dlq_redrive_error",
                },
            )
            if stop_on_error:
                logger.warning(
                    "dlq_redrive_stopped",
                    extra={
                        "reason": "stop_on_error",
                        "record_id": record.record_id,
                        "event": "dlq_redrive_stop",
                    },
                )
                break

    logger.info(
        "dlq_redrive_completed",
        extra={
            "results": results,
            "event": "dlq_redrive_complete",
        },
    )

    return results