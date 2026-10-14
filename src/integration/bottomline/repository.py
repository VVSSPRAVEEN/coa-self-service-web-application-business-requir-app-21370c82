"""Repository layer for persisting ingested Bottomline COA records."""

from __future__ import annotations


from typing import Dict, List, Optional, Set, Tuple

from src.integration.bottomline.models import (
    AuditEntry,
    CostObjectLineItem,
    IngestedFile,
    IngestionStatus,
)


class IngestionRepository:
    """In-memory repository for ingestion state management.

    In production this would be backed by PostgreSQL via SQLAlchemy.
    This implementation provides the contract for deduplication, persistence,
    and audit trail operations required by the ingestion pipeline.
    """

    def __init__(self) -> None:
        self._files: Dict[str, IngestedFile] = {}
        self._records: Dict[str, List[CostObjectLineItem]] = {}
        self._audit_log: List[AuditEntry] = []
        self._seen_keys: Set[Tuple[str, str]] = set()

    def is_duplicate(self, batch_id: str, file_name: str) -> bool:
        """Check if a file with this BatchID/FileName has already been ingested.

        Args:
            batch_id: The batch identifier.
            file_name: The original file name.

        Returns:
            True if the combination has been seen before.
        """
        return (batch_id, file_name) in self._seen_keys

    def mark_seen(self, batch_id: str, file_name: str) -> None:
        """Mark a BatchID/FileName combination as seen for deduplication."""
        self._seen_keys.add((batch_id, file_name))

    def save_file_metadata(self, ingested_file: IngestedFile) -> None:
        """Persist file-level ingestion metadata."""
        self._files[ingested_file.batch_id] = ingested_file

    def get_file_metadata(self, batch_id: str) -> Optional[IngestedFile]:
        """Retrieve file metadata by batch ID."""
        return self._files.get(batch_id)

    def save_records(
        self, batch_id: str, records: List[CostObjectLineItem]
    ) -> None:
        """Persist canonical COA line item records for a batch."""
        existing = self._records.get(batch_id, [])
        existing.extend(records)
        self._records[batch_id] = existing

    def get_records(self, batch_id: str) -> List[CostObjectLineItem]:
        """Retrieve all records for a given batch."""
        return list(self._records.get(batch_id, []))

    def add_audit_entry(self, entry: AuditEntry) -> None:
        """Append a structured audit entry to the immutable audit log."""
        self._audit_log.append(entry)

    def get_audit_entries(
        self, batch_id: Optional[str] = None
    ) -> List[AuditEntry]:
        """Retrieve audit entries, optionally filtered by batch ID."""
        if batch_id is None:
            return list(self._audit_log)
        return [e for e in self._audit_log if e.batch_id == batch_id]

    def update_file_status(
        self,
        batch_id: str,
        status: IngestionStatus,
        error_reason: Optional[str] = None,
    ) -> None:
        """Update the ingestion status of a file."""
        existing = self._files.get(batch_id)
        if existing is not None:
            self._files[batch_id] = IngestedFile(
                batch_id=existing.batch_id,
                file_name=existing.file_name,
                checksum_sha256=existing.checksum_sha256,
                status=status,
                record_count=existing.record_count,
                ingested_at=existing.ingested_at,
                error_reason=error_reason or existing.error_reason,
            )