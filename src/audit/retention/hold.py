"""Legal hold management for COA retention exemption."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set


@dataclass
class LegalHoldRecord:
    """Represents a legal hold placed on a specific resource."""

    resource_id: str
    reason: str
    placed_by: str
    placed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    released_at: Optional[datetime] = None
    released_by: Optional[str] = None
    is_active: bool = True

    def release(self, released_by: str) -> None:
        """Mark this hold as released."""
        self.is_active = False
        self.released_at = datetime.now(timezone.utc)
        self.released_by = released_by


class LegalHoldManager:
    """Manages legal holds that prevent purging of COAs and audit records.

    Thread-safe in-memory implementation. In production, backed by
    the RDS PostgreSQL legal_holds table.
    """

    def __init__(self) -> None:
        self._holds: Dict[str, List[LegalHoldRecord]] = {}
        self._lock = threading.Lock()

    def place_hold(
        self,
        resource_id: str,
        reason: str,
        placed_by: str,
        allowed_reasons: Optional[Set[str]] = None,
    ) -> LegalHoldRecord:
        """Place a legal hold on a resource.

        Args:
            resource_id: The COA or resource identifier.
            reason: Justification for the hold.
            placed_by: User subject who placed the hold.
            allowed_reasons: Optional set of valid reason strings.

        Returns:
            The created LegalHoldRecord.

        Raises:
            ValueError: If the reason is not in the allowed set.
        """
        if allowed_reasons is not None and reason not in allowed_reasons:
            raise ValueError(
                f"Invalid hold reason '{reason}'. "
                f"Allowed: {sorted(allowed_reasons)}"
            )

        record = LegalHoldRecord(
            resource_id=resource_id,
            reason=reason,
            placed_by=placed_by,
        )

        with self._lock:
            if resource_id not in self._holds:
                self._holds[resource_id] = []
            self._holds[resource_id].append(record)

        return record

    def release_hold(
        self,
        resource_id: str,
        released_by: str,
        reason: Optional[str] = None,
    ) -> int:
        """Release active legal holds on a resource.

        Args:
            resource_id: The resource to release holds for.
            released_by: User subject releasing the hold.
            reason: If specified, only release holds matching this reason.

        Returns:
            Number of holds released.
        """
        released_count = 0
        with self._lock:
            holds = self._holds.get(resource_id, [])
            for hold in holds:
                if hold.is_active:
                    if reason is None or hold.reason == reason:
                        hold.release(released_by)
                        released_count += 1
        return released_count

    def is_on_hold(self, resource_id: str) -> bool:
        """Check if a resource has any active legal holds."""
        with self._lock:
            holds = self._holds.get(resource_id, [])
            return any(h.is_active for h in holds)

    def get_active_holds(self, resource_id: str) -> List[LegalHoldRecord]:
        """Get all active legal holds for a resource."""
        with self._lock:
            holds = self._holds.get(resource_id, [])
            return [h for h in holds if h.is_active]

    def get_all_held_resources(self) -> Set[str]:
        """Return all resource IDs that currently have active holds."""
        with self._lock:
            return {
                rid
                for rid, holds in self._holds.items()
                if any(h.is_active for h in holds)
            }

    def get_hold_history(self, resource_id: str) -> List[LegalHoldRecord]:
        """Return full hold history including released holds."""
        with self._lock:
            return list(self._holds.get(resource_id, []))
