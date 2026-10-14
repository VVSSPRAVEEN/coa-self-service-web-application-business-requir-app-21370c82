"""Dead Letter Queue implementation for durable failure recording."""

from __future__ import annotations

import json
import logging
import os
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Metrics counters
METRICS: dict[str, int] = {
    "dlq_records_written": 0,
    "dlq_records_redriven": 0,
    "dlq_write_errors": 0,
}


@dataclass
class DLQRecord:
    """A record stored in the Dead Letter Queue.

    Attributes:
        record_id: Unique identifier for idempotency.
        payload: The original payload that failed processing.
        attempt_count: Number of attempts made before giving up.
        error_type: The class name of the exception.
        error_message: The string representation of the exception.
        stack_trace: Full traceback of the failure.
        timestamp: ISO 8601 timestamp of when the record was written.
        source: Identifier of the source system/pipeline stage.
    """

    record_id: str
    payload: Any
    attempt_count: int
    error_type: str
    error_message: str
    stack_trace: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "ingestion"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the record to a dictionary."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize the record to a JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DLQRecord:
        """Deserialize a record from a dictionary."""
        return cls(
            record_id=data["record_id"],
            payload=data["payload"],
            attempt_count=data["attempt_count"],
            error_type=data["error_type"],
            error_message=data["error_message"],
            stack_trace=data["stack_trace"],
            timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            source=data.get("source", "ingestion"),
        )


class DeadLetterQueue:
    """Durable Dead Letter Queue that records exhausted or unrecoverable failures.

    Records are persisted to a file-based store for durability in the
    self-hosted Docker Compose environment.
    """

    def __init__(self, storage_path: str | None = None) -> None:
        """Initialize the DLQ with a storage path.

        Args:
            storage_path: Directory path for DLQ storage. Defaults to
                DLQ_STORAGE_PATH env var or ./dlq_store.
        """
        base_path = storage_path or os.environ.get("DLQ_STORAGE_PATH", "./dlq_store")
        # Use Path.name for safe path derivation - never interpolate untrusted input
        safe_dir_name = Path(base_path).name if Path(base_path).name else "dlq_store"
        self._storage_dir = Path(base_path).parent / safe_dir_name
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._records: list[DLQRecord] = []
        self._processed_ids: set[str] = set()
        self._load_existing_records()

    def _load_existing_records(self) -> None:
        """Load existing DLQ records from storage on initialization."""
        dlq_file = self._storage_dir / "dead_letters.jsonl"
        if dlq_file.exists():
            try:
                with open(dlq_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("redriven"):
                            self._processed_ids.add(f"redriven:{data['record_id']}")
                            continue
                        record = DLQRecord.from_dict(data)
                        self._records.append(record)
                        self._processed_ids.add(record.record_id)
                logger.info(
                    "dlq_loaded",
                    extra={"record_count": len(self._records), "event": "dlq_init"},
                )
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.error(
                    "dlq_load_error",
                    extra={"error": str(e), "event": "dlq_init_error"},
                )
                METRICS["dlq_write_errors"] += 1

    def write(
        self,
        record_id: str,
        payload: Any,
        attempt_count: int,
        error: BaseException,
        source: str = "ingestion",
    ) -> DLQRecord:
        """Durably record a failed item to the dead letter queue.

        Args:
            record_id: Unique idempotency key for the record.
            payload: The original payload that failed.
            attempt_count: How many attempts were made.
            error: The exception that caused the final failure.
            source: Source pipeline identifier.

        Returns:
            The created DLQRecord.
        """
        tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

        record = DLQRecord(
            record_id=record_id,
            payload=payload,
            attempt_count=attempt_count,
            error_type=type(error).__name__,
            error_message=str(error),
            stack_trace=tb_str,
            source=source,
        )

        self._persist_record(record)
        self._records.append(record)
        self._processed_ids.add(record_id)
        METRICS["dlq_records_written"] += 1

        logger.error(
            "dlq_record_written",
            extra={
                "record_id": record_id,
                "attempt_count": attempt_count,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "source": source,
                "event": "dlq_write",
                "metric_dlq_records_written": METRICS["dlq_records_written"],
            },
        )

        return record

    def _persist_record(self, record: DLQRecord) -> None:
        """Persist a single record to the DLQ file store.

        Args:
            record: The DLQ record to persist.
        """
        dlq_file = self._storage_dir / "dead_letters.jsonl"
        try:
            with open(dlq_file, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except OSError as e:
            logger.critical(
                "dlq_persist_failure",
                extra={
                    "record_id": record.record_id,
                    "error": str(e),
                    "event": "dlq_persist_error",
                },
            )
            METRICS["dlq_write_errors"] += 1
            raise RuntimeError(f"Failed to persist DLQ record {record.record_id}: {e}") from e

    def is_processed(self, record_id: str) -> bool:
        """Check if a record ID has already been processed (for idempotency).

        Args:
            record_id: The unique record identifier.

        Returns:
            True if the record has been seen before.
        """
        return record_id in self._processed_ids

    def mark_redriven(self, record_id: str) -> None:
        """Mark a record as successfully redriven.

        The marker is appended to the same JSONL store the records live in, so a
        later process - or the next cycle of a scheduled redrive - does not
        replay the record again. Redrive is exactly-once by contract (see
        :func:`src.integration.retry.redrive.redrive_dlq`), which only holds if
        this state outlives the process that set it.

        Args:
            record_id: The unique record identifier.
        """
        self._persist_marker(record_id)
        self._processed_ids.add(f"redriven:{record_id}")
        METRICS["dlq_records_redriven"] += 1
        logger.info(
            "dlq_record_redriven",
            extra={
                "record_id": record_id,
                "event": "dlq_redrive",
                "metric_dlq_records_redriven": METRICS["dlq_records_redriven"],
            },
        )

    def _persist_marker(self, record_id: str) -> None:
        """Append a redrive marker line to the DLQ store.

        A marker is a JSON object carrying ``record_id`` and ``redriven: true``.
        :meth:`_load_existing_records` recognises that shape and skips it, so a
        store written by any version of this module stays readable.
        """
        dlq_file = self._storage_dir / "dead_letters.jsonl"
        marker = json.dumps(
            {
                "record_id": record_id,
                "redriven": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        try:
            with open(dlq_file, "a", encoding="utf-8") as f:
                f.write(marker + "\n")
        except OSError as e:
            logger.critical(
                "dlq_marker_persist_failure",
                extra={
                    "record_id": record_id,
                    "error": str(e),
                    "event": "dlq_marker_persist_error",
                },
            )
            METRICS["dlq_write_errors"] += 1
            raise RuntimeError(
                f"Failed to persist redrive marker for {record_id}: {e}"
            ) from e

    def get_all_records(self) -> list[DLQRecord]:
        """Retrieve all records currently in the DLQ.

        Returns:
            List of all DLQ records.
        """
        return list(self._records)

    def get_unredriven_records(self) -> list[DLQRecord]:
        """Retrieve records that have not yet been successfully redriven.

        Returns:
            List of DLQ records pending redrive.
        """
        return [
            r for r in self._records
            if f"redriven:{r.record_id}" not in self._processed_ids
        ]

    def get_metrics(self) -> dict[str, int]:
        """Return current DLQ metrics counters.

        Returns:
            Dictionary of metric names to values.
        """
        return dict(METRICS)
