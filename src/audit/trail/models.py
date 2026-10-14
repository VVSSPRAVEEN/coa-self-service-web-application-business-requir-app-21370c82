"""Data models for the immutable audit trail."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class AuditEventType(str, Enum):
    """Types of auditable events in the COA lifecycle."""

    GENERATION = "generation"
    ACCESS = "access"
    DOWNLOAD = "download"
    PRINT = "print"
    LEGAL_HOLD_APPLIED = "legal_hold_applied"
    LEGAL_HOLD_RELEASED = "legal_hold_released"
    EXPORT = "export"
    PURGE = "purge"


class AuditLogEntry(BaseModel):
    """An immutable, append-only audit log entry.

    Each entry is hash-chained to the previous one to provide
    tamper-evident integrity verification.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AuditEventType
    user_sub: str = Field(..., description="Subject identifier of the actor")
    resource_id: str = Field(..., description="Identifier of the target COA or resource")
    details: str | None = None
    ip_address: str | None = None
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    previous_hash: str = Field(
        default="GENESIS",
        description="SHA-256 hash of the previous entry; GENESIS for the first entry",
    )
    entry_hash: str = Field(
        default="",
        description="SHA-256 hash of this entry's canonical payload",
    )
    sequence_number: int = Field(
        default=0,
        ge=0,
        description="Monotonically increasing sequence number",
    )

    def compute_canonical_payload(self) -> str:
        """Return a deterministic string representation for hashing.

        Excludes entry_hash itself so that the hash can be computed
        over the remaining fields.
        """
        parts = [
            self.event_id,
            self.event_type.value,
            self.user_sub,
            self.resource_id,
            self.details or "",
            self.ip_address or "",
            self.timestamp.isoformat(),
            self.previous_hash,
            str(self.sequence_number),
        ]
        return "|".join(parts)

    class Config:
        frozen = True
