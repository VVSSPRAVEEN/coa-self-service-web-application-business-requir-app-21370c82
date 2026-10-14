"""Error quarantine handling for rejected Bottomline inbound files (FR17).

A file that fails parsing or record-level validation is never dropped silently:
it is copied into the quarantine directory verbatim and a structured audit
entry is recorded against the batch, so an operator can inspect the payload and
redrive the batch later.

Output paths are derived with :meth:`pathlib.Path.name`, so a caller-supplied
file name can never escape the quarantine directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.integration.bottomline.models import AuditEntry
from src.integration.bottomline.repository import IngestionRepository

DEFAULT_QUARANTINE_DIR = "/tmp/quarantine"


@dataclass(frozen=True)
class QuarantineResult:
    """Result of quarantining a rejected file."""

    quarantine_path: str
    reason: str
    timestamp: datetime


def quarantine_rejected_file(
    batch_id: str,
    file_name: str,
    content: bytes,
    reason: str,
    repository: IngestionRepository,
    quarantine_dir: str | None = None,
) -> QuarantineResult:
    """Route a rejected file to the error quarantine with a structured audit entry.

    Args:
        batch_id: The batch identifier for the rejected file.
        file_name: Original file name; only its base name is used.
        content: Raw file content bytes.
        reason: Human-readable rejection reason.
        repository: Repository used to record the audit entry.
        quarantine_dir: Directory for quarantined files. Defaults to
            ``DEFAULT_QUARANTINE_DIR``.

    Returns:
        QuarantineResult carrying the quarantine path, reason and timestamp.
    """
    safe_name = Path(file_name).name
    if not safe_name:
        safe_name = f"unknown_{batch_id}.dat"

    target_dir = Path(quarantine_dir or DEFAULT_QUARANTINE_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC)
    ts_suffix = timestamp.strftime("%Y%m%dT%H%M%S")
    quarantine_name = f"{batch_id}_{ts_suffix}_{safe_name}"
    quarantine_path = target_dir / quarantine_name

    quarantine_path.write_bytes(content)

    audit_entry = AuditEntry(
        batch_id=batch_id,
        file_name=safe_name,
        action="QUARANTINE",
        status="REJECTED",
        details={
            "reason": reason,
            "quarantine_path": str(quarantine_path),
            "file_size_bytes": len(content),
        },
        timestamp=timestamp,
    )
    repository.add_audit_entry(audit_entry)

    return QuarantineResult(
        quarantine_path=str(quarantine_path),
        reason=reason,
        timestamp=timestamp,
    )
