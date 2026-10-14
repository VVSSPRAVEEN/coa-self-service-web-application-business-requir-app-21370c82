"""Main Bottomline ingestion orchestrator."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from src.integration.bottomline.models import (
    AuditEntry,
    IngestedFile,
    IngestionStatus,
    compute_checksum,
)
from src.integration.bottomline.parser import BottomlineParseError, parse_bottomline_flat
from src.integration.bottomline.quarantine import quarantine_rejected_file
from src.integration.bottomline.repository import IngestionRepository
from src.integration.bottomline.sftp_poller import SftpFileEntry, SftpPoller
from src.integration.bottomline.transformer import (
    TransformationError,
    transform_to_coa_records,
)

logger = logging.getLogger(__name__)


class BottomlineIngestor:
    """Orchestrates the Bottomline inbound file ingestion pipeline.

    Reads files from SFTP, validates and de-duplicates by BatchID/FileName,
    parses them into canonical inbound COA records, applies the format
    transformation, persists through the repository layer, and routes
    rejected files to the error quarantine with structured audit entries.
    """

    def __init__(
        self,
        repository: IngestionRepository,
        poller: Optional[SftpPoller] = None,
        quarantine_dir: Optional[str] = None,
    ) -> None:
        self._repository = repository
        self._poller = poller
        self._quarantine_dir = quarantine_dir

    def ingest_file(self, entry: SftpFileEntry) -> IngestedFile:
        """Process a single Bottomline inbound file through the full pipeline.

        Args:
            entry: The SFTP file entry to ingest.

        Returns:
            IngestedFile metadata reflecting the outcome.
        """
        batch_id = entry.batch_id
        file_name = entry.file_name
        content = entry.content
        checksum = compute_checksum(content)
        now = datetime.utcnow()

        self._repository.add_audit_entry(
            AuditEntry(
                batch_id=batch_id,
                file_name=file_name,
                action="INGEST_START",
                status="PENDING",
                details={"checksum": checksum, "size_bytes": len(content)},
                timestamp=now,
            )
        )

        if self._repository.is_duplicate(batch_id, file_name):
            duplicate_file = IngestedFile(
                batch_id=batch_id,
                file_name=file_name,
                checksum_sha256=checksum,
                status=IngestionStatus.DUPLICATE,
                record_count=0,
                ingested_at=now,
                error_reason="Duplicate BatchID/FileName combination",
            )
            self._repository.save_file_metadata(duplicate_file)
            self._repository.add_audit_entry(
                AuditEntry(
                    batch_id=batch_id,
                    file_name=file_name,
                    action="DUPLICATE_DETECTED",
                    status="DUPLICATE",
                    timestamp=now,
                )
            )
            logger.info("Duplicate file detected: %s/%s", batch_id, file_name)
            return duplicate_file

        self._repository.mark_seen(batch_id, file_name)
        text_content = content.decode("utf-8", errors="replace")

        try:
            raw_records = parse_bottomline_flat(text_content, batch_id)
        except BottomlineParseError as exc:
            return self._handle_rejection(
                batch_id=batch_id,
                file_name=file_name,
                content=content,
                checksum=checksum,
                reason=f"Parse error: {exc}",
                now=now,
            )

        try:
            coa_records = transform_to_coa_records(raw_records)
        except TransformationError as exc:
            return self._handle_rejection(
                batch_id=batch_id,
                file_name=file_name,
                content=content,
                checksum=checksum,
                reason=f"Transformation error: {exc}",
                now=now,
            )

        self._repository.save_records(batch_id, coa_records)

        ingested_file = IngestedFile(
            batch_id=batch_id,
            file_name=file_name,
            checksum_sha256=checksum,
            status=IngestionStatus.PERSISTED,
            record_count=len(coa_records),
            ingested_at=now,
        )
        self._repository.save_file_metadata(ingested_file)

        self._repository.add_audit_entry(
            AuditEntry(
                batch_id=batch_id,
                file_name=file_name,
                action="INGEST_COMPLETE",
                status="PERSISTED",
                details={"record_count": len(coa_records)},
                timestamp=now,
            )
        )

        logger.info(
            "Successfully ingested %d records from %s/%s",
            len(coa_records),
            batch_id,
            file_name,
        )
        return ingested_file

    def poll_and_ingest(self) -> list[IngestedFile]:
        """Poll SFTP for new files and ingest each one.

        Returns:
            List of IngestedFile results.
        """
        if self._poller is None:
            logger.warning("No SFTP poller configured; skipping poll cycle")
            return []

        entries = self._poller.poll()
        results: list[IngestedFile] = []

        for entry in entries:
            result = self.ingest_file(entry)
            results.append(result)
            if result.status == IngestionStatus.PERSISTED:
                self._poller.acknowledge(entry.file_name)

        return results

    def _handle_rejection(
        self,
        batch_id: str,
        file_name: str,
        content: bytes,
        checksum: str,
        reason: str,
        now: datetime,
    ) -> IngestedFile:
        """Handle a rejected file by quarantining it and recording audit."""
        quarantine_rejected_file(
            batch_id=batch_id,
            file_name=file_name,
            content=content,
            reason=reason,
            repository=self._repository,
            quarantine_dir=self._quarantine_dir,
        )

        rejected_file = IngestedFile(
            batch_id=batch_id,
            file_name=file_name,
            checksum_sha256=checksum,
            status=IngestionStatus.QUARANTINED,
            record_count=0,
            ingested_at=now,
            error_reason=reason,
        )
        self._repository.save_file_metadata(rejected_file)

        self._repository.add_audit_entry(
            AuditEntry(
                batch_id=batch_id,
                file_name=file_name,
                action="QUARANTINE",
                status="REJECTED",
                details={"reason": reason},
                timestamp=now,
            )
        )

        logger.warning("File quarantined: %s/%s - %s", batch_id, file_name, reason)
        return rejected_file
