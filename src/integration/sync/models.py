"""Models for near-real-time COA status synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class CoaStatus(str, Enum):
    """Monotonic COA lifecycle statuses.

    Statuses are ordered; transitions may only move forward, never regress.
    Terminal states cannot be transitioned out of.
    """

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


# Monotonic ordering: index represents the progression level
STATUS_TRANSITION_ORDER: List[CoaStatus] = [
    CoaStatus.DRAFT,
    CoaStatus.SUBMITTED,
    CoaStatus.UNDER_REVIEW,
    CoaStatus.APPROVED,
    CoaStatus.COMPLETED,
    CoaStatus.ARCHIVED,
]

# Terminal states that must never regress
TERMINAL_STATUSES: frozenset[CoaStatus] = frozenset(
    {CoaStatus.COMPLETED, CoaStatus.ARCHIVED, CoaStatus.REJECTED}
)

# Valid transition map: from_status -> set of allowed next statuses
VALID_TRANSITIONS: Dict[CoaStatus, frozenset[CoaStatus]] = {
    CoaStatus.DRAFT: frozenset({CoaStatus.SUBMITTED, CoaStatus.REJECTED}),
    CoaStatus.SUBMITTED: frozenset({CoaStatus.UNDER_REVIEW, CoaStatus.REJECTED}),
    CoaStatus.UNDER_REVIEW: frozenset({CoaStatus.APPROVED, CoaStatus.REJECTED}),
    CoaStatus.APPROVED: frozenset({CoaStatus.COMPLETED}),
    CoaStatus.REJECTED: frozenset(),  # Terminal
    CoaStatus.COMPLETED: frozenset({CoaStatus.ARCHIVED}),
    CoaStatus.ARCHIVED: frozenset(),  # Terminal
}


@dataclass(frozen=True)
class StatusDelta:
    """A status change event from JDE or Bottomline.

    Attributes:
        coa_number: The COA identifier.
        new_status: The proposed new status.
        source: Origin system ('JDE' or 'BOTTOMLINE').
        sequence_id: Monotonically increasing sequence for ordering.
        timestamp: When the delta was generated.
        idempotency_key: Unique key for idempotent processing.
    """

    coa_number: str
    new_status: CoaStatus
    source: str
    sequence_id: int
    timestamp: datetime
    idempotency_key: str


@dataclass
class CoaHeader:
    """Current state of a COA header record.

    Attributes:
        coa_number: The COA identifier.
        current_status: Current lifecycle status.
        last_sequence_id: Highest sequence_id applied (for out-of-order safety).
        last_updated: When the status was last changed.
        processed_keys: Set of idempotency keys already applied.
    """

    coa_number: str
    current_status: CoaStatus = CoaStatus.DRAFT
    last_sequence_id: int = 0
    last_updated: datetime = field(default_factory=datetime.utcnow)
    processed_keys: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class SyncResult:
    """Result of applying a status delta.

    Attributes:
        coa_number: The COA that was processed.
        previous_status: Status before the delta.
        new_status: Status after the delta.
        applied: Whether the transition was actually applied.
        reason: Explanation if not applied.
    """

    coa_number: str
    previous_status: CoaStatus
    new_status: CoaStatus
    applied: bool
    reason: Optional[str] = None
