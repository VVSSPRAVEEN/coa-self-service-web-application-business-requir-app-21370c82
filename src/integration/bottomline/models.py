"""Data models for Bottomline ingestion pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class IngestionStatus(str, Enum):
    """Status of an ingested file."""

    PENDING = "PENDING"
    PARSED = "PARSED"
    TRANSFORMED = "TRANSFORMED"
    PERSISTED = "PERSISTED"
    QUARANTINED = "QUARANTINED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class BottomlineRawRecord:
    """A single parsed record from a Bottomline flat file."""

    batch_id: str
    coa_number: str
    product_code: str
    lot_number: str
    test_code: str
    test_result: str
    test_unit: str
    specification_min: Optional[float]
    specification_max: Optional[float]
    recorded_at: datetime
    raw_line: str
    line_number: int


@dataclass(frozen=True)
class CostObjectLineItem:
    """Canonical internal line item model transformed from Bottomline format."""

    coa_number: str
    product_code: str
    lot_number: str
    test_code: str
    test_result: str
    test_unit: str
    specification_min: Optional[float]
    specification_max: Optional[float]
    recorded_at: datetime
    source_batch_id: str
    source_line_number: int


@dataclass(frozen=True)
class IngestedFile:
    """Metadata for a fully ingested Bottomline file."""

    batch_id: str
    file_name: str
    checksum_sha256: str
    status: IngestionStatus
    record_count: int
    ingested_at: datetime
    error_reason: Optional[str] = None


@dataclass
class AuditEntry:
    """Structured audit entry for ingestion events."""

    batch_id: str
    file_name: str
    action: str
    status: str
    details: dict[str, object] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


def compute_checksum(data: bytes) -> str:
    """Compute SHA-256 checksum of file content."""
    return hashlib.sha256(data).hexdigest()
