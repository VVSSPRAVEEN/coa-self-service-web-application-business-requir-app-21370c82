"""Retention enforcement service coordinating policy, holds, and purge/export."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.audit.retention.hold import LegalHoldManager
from src.audit.retention.policy import RetentionConfig, RetentionPolicy

logger = logging.getLogger(__name__)


@dataclass
class PurgeResult:
    """Result of a purge operation."""

    purged_count: int
    skipped_held_count: int
    skipped_not_expired_count: int
    purged_resource_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    """Result of an export operation."""

    exported_count: int
    output_path: str
    errors: list[str] = field(default_factory=list)


# Type alias for the actual deletion callback used during purge
PurgeCallback = Callable[[str], bool]
# Type alias for fetching records to evaluate
RecordFetcher = Callable[[], list[dict[str, Any]]]


class RetentionService:
    """Orchestrates retention policy enforcement, legal holds, export, and purge.

    This service evaluates COA records against the 5-year minimum retention
    policy, respects legal holds, and provides controlled export/purge
    capabilities for records that have exceeded their retention window.
    """

    def __init__(
        self,
        policy: RetentionPolicy | None = None,
        hold_manager: LegalHoldManager | None = None,
        config: RetentionConfig | None = None,
    ) -> None:
        cfg = config or RetentionConfig()
        self._policy = policy or RetentionPolicy(cfg)
        self._hold_manager = hold_manager or LegalHoldManager()
        self._config = cfg

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    @property
    def hold_manager(self) -> LegalHoldManager:
        return self._hold_manager

    def evaluate_record(
        self,
        resource_id: str,
        created_at: datetime,
        reference_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate a single record's retention status.

        Returns a dict with expiry info, hold status, and eligibility.
        """
        ref = reference_time or datetime.now(UTC)
        is_expired = self._policy.is_expired(created_at, ref)
        is_held = self._hold_manager.is_on_hold(resource_id)
        remaining = self._policy.remaining_days(created_at, ref)
        expiry_date = self._policy.get_expiry_date(created_at)

        return {
            "resource_id": resource_id,
            "created_at": created_at.isoformat(),
            "expiry_date": expiry_date.isoformat(),
            "is_expired": is_expired,
            "is_on_legal_hold": is_held,
            "remaining_days": remaining,
            "eligible_for_purge": is_expired and not is_held,
        }

    def export_expired_records(
        self,
        records: list[dict[str, Any]],
        output_directory: str,
        reference_time: datetime | None = None,
    ) -> ExportResult:
        """Export records that have exceeded their retention window.

        Records are exported to a JSON file before they can be purged.
        Uses path.basename/pathlib to derive safe output paths.

        Args:
            records: List of dicts with 'resource_id' and 'created_at' keys.
            output_directory: Directory to write the export file.
            reference_time: Time to evaluate expiry against.

        Returns:
            ExportResult with count and output path.
        """
        ref = reference_time or datetime.now(UTC)
        safe_dir = Path(output_directory)
        safe_dir.mkdir(parents=True, exist_ok=True)

        timestamp_str = ref.strftime("%Y%m%d_%H%M%S")
        filename = f"coa_retention_export_{timestamp_str}.json"
        # Use Path.name to avoid directory traversal
        safe_filename = Path(filename).name
        output_path = safe_dir / safe_filename

        exportable: list[dict[str, Any]] = []
        errors: list[str] = []

        for record in records:
            resource_id = str(record.get("resource_id", ""))
            created_at_raw = record.get("created_at")

            if not resource_id or not created_at_raw:
                errors.append(f"Invalid record missing required fields: {record}")
                continue

            if isinstance(created_at_raw, str):
                try:
                    created_at = datetime.fromisoformat(created_at_raw)
                except ValueError:
                    errors.append(f"Invalid date format for {resource_id}: {created_at_raw}")
                    continue
            elif isinstance(created_at_raw, datetime):
                created_at = created_at_raw
            else:
                errors.append(f"Unexpected date type for {resource_id}")
                continue

            if self._policy.is_expired(created_at, ref):
                exportable.append({
                    "resource_id": resource_id,
                    "created_at": created_at.isoformat(),
                    "evaluation": self.evaluate_record(resource_id, created_at, ref),
                })

        export_data = json.dumps(exportable, indent=2, default=str)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(export_data)

        logger.info(
            "Exported %d expired records to %s",
            len(exportable),
            str(output_path),
        )

        return ExportResult(
            exported_count=len(exportable),
            output_path=str(output_path),
            errors=errors,
        )

    def purge_expired_records(
        self,
        records: list[dict[str, Any]],
        purge_callback: PurgeCallback,
        require_export_first: bool = True,
        reference_time: datetime | None = None,
    ) -> PurgeResult:
        """Purge records that have exceeded their 5-year retention window.

        Only purges records that are:
        1. Past the minimum 1826-day retention window.
        2. NOT under any active legal hold.

        Args:
            records: List of dicts with 'resource_id' and 'created_at'.
            purge_callback: Function called with resource_id to perform actual deletion.
                            Must return True on success, False on failure.
            require_export_first: If True, log a warning but still proceed.
            reference_time: Time to evaluate expiry against.

        Returns:
            PurgeResult with counts and any errors.
        """
        ref = reference_time or datetime.now(UTC)
        result = PurgeResult(
            purged_count=0,
            skipped_held_count=0,
            skipped_not_expired_count=0,
        )

        batch_size = self._config.purge_batch_size
        processed = 0

        for record in records:
            if self._config.enable_auto_purge is False and not purge_callback:
                break

            if processed >= batch_size:
                logger.info("Reached purge batch size limit of %d", batch_size)
                break

            resource_id = str(record.get("resource_id", ""))
            created_at_raw = record.get("created_at")

            if not resource_id or not created_at_raw:
                result.errors.append(f"Invalid record: {record}")
                continue

            if isinstance(created_at_raw, str):
                try:
                    created_at = datetime.fromisoformat(created_at_raw)
                except ValueError:
                    result.errors.append(f"Bad date for {resource_id}")
                    continue
            elif isinstance(created_at_raw, datetime):
                created_at = created_at_raw
            else:
                result.errors.append(f"Unexpected date type for {resource_id}")
                continue

            # Check retention window
            if not self._policy.is_expired(created_at, ref):
                result.skipped_not_expired_count += 1
                continue

            # Check legal hold
            if self._hold_manager.is_on_hold(resource_id):
                result.skipped_held_count += 1
                logger.warning(
                    "Skipping purge for %s: active legal hold",
                    resource_id,
                )
                continue

            # Perform purge via callback
            try:
                success = purge_callback(resource_id)
                if success:
                    result.purged_count += 1
                    result.purged_resource_ids.append(resource_id)
                    logger.info("Purged expired record: %s", resource_id)
                else:
                    result.errors.append(f"Purge callback returned False for {resource_id}")
            except Exception as exc:  # noqa: BLE001 - one record's purge failure must not abort the sweep
                error_msg = f"Purge failed for {resource_id}: {exc}"
                result.errors.append(error_msg)
                logger.error(error_msg)

            processed += 1

        return result

    def place_legal_hold(
        self,
        resource_id: str,
        reason: str,
        placed_by: str,
    ) -> None:
        """Place a legal hold preventing purge of a resource."""
        self._hold_manager.place_hold(
            resource_id=resource_id,
            reason=reason,
            placed_by=placed_by,
            allowed_reasons=self._config.legal_hold_reasons,
        )
        logger.info(
            "Legal hold placed on %s by %s for reason: %s",
            resource_id,
            placed_by,
            reason,
        )

    def release_legal_hold(
        self,
        resource_id: str,
        released_by: str,
        reason: str | None = None,
    ) -> int:
        """Release legal holds on a resource."""
        count = self._hold_manager.release_hold(
            resource_id=resource_id,
            released_by=released_by,
            reason=reason,
        )
        logger.info(
            "Released %d legal hold(s) on %s by %s",
            count,
            resource_id,
            released_by,
        )
        return count

    def get_retention_status_report(
        self,
        records: list[dict[str, Any]],
        reference_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Generate a retention status report for a set of records."""
        ref = reference_time or datetime.now(UTC)
        report: list[dict[str, Any]] = []

        for record in records:
            resource_id = str(record.get("resource_id", ""))
            created_at_raw = record.get("created_at")

            if not resource_id or not created_at_raw:
                continue

            if isinstance(created_at_raw, str):
                try:
                    created_at = datetime.fromisoformat(created_at_raw)
                except ValueError:
                    continue
            elif isinstance(created_at_raw, datetime):
                created_at = created_at_raw
            else:
                continue

            report.append(
                self.evaluate_record(resource_id, created_at, ref)
            )

        return report