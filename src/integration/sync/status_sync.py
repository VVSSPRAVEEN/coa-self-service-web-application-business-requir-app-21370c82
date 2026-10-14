"""Near-real-time COA status synchronization service.

Subscribes/consumes status deltas from JDE and Bottomline (via webhook
endpoints and scheduled reconciliation polls), resolves them against
existing COA headers, and applies monotonic status transitions (never
regressing a terminal state) with idempotent, out-of-order-safe handling.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from src.integration.sync.models import (
    VALID_TRANSITIONS,
    CoaHeader,
    CoaStatus,
    StatusDelta,
    SyncResult,
    TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)


class IllegalTransitionError(Exception):
    """Raised when a status transition violates monotonic ordering rules."""


class StatusSyncService:
    """Manages near-real-time COA status synchronization.

    Ensures:
    - Monotonic status transitions (never regressing a terminal state)
    - Idempotent processing (duplicate deltas are safely ignored)
    - Out-of-order safety (late-arriving deltas with lower sequence IDs
      are evaluated but do not regress state)
    """

    def __init__(self) -> None:
        self._headers: Dict[str, CoaHeader] = {}
        self._sync_history: List[SyncResult] = []

    def get_or_create_header(self, coa_number: str) -> CoaHeader:
        """Get existing COA header or create a new one in DRAFT status."""
        if coa_number not in self._headers:
            self._headers[coa_number] = CoaHeader(
                coa_number=coa_number,
                current_status=CoaStatus.DRAFT,
                last_sequence_id=0,
                last_updated=datetime.utcnow(),
                processed_keys=set(),
            )
        return self._headers[coa_number]

    def apply_delta(self, delta: StatusDelta) -> SyncResult:
        """Apply a single status delta with idempotent, out-of-order-safe handling.

        Args:
            delta: The status change event to process.

        Returns:
            SyncResult indicating whether the transition was applied.
        """
        header = self.get_or_create_header(delta.coa_number)
        previous_status = header.current_status

        # Idempotency check: skip if we've already processed this exact delta
        if delta.idempotency_key in header.processed_keys:
            result = SyncResult(
                coa_number=delta.coa_number,
                previous_status=previous_status,
                new_status=previous_status,
                applied=False,
                reason="Idempotent duplicate: delta already processed",
            )
            self._sync_history.append(result)
            logger.debug(
                "Skipping duplicate delta for %s: %s",
                delta.coa_number,
                delta.idempotency_key,
            )
            return result

        # Terminal state guard: never regress from a terminal state
        if previous_status in TERMINAL_STATUSES:
            result = SyncResult(
                coa_number=delta.coa_number,
                previous_status=previous_status,
                new_status=previous_status,
                applied=False,
                reason=f"Cannot transition from terminal state {previous_status.value}",
            )
            self._sync_history.append(result)
            logger.warning(
                "Rejected transition for %s: terminal state %s",
                delta.coa_number,
                previous_status.value,
            )
            return result

        # Validate the transition is legal per the state machine
        allowed = VALID_TRANSITIONS.get(previous_status, frozenset())
        if delta.new_status not in allowed:
            # Out-of-order safety: if the delta's sequence is older than what
            # we've already processed, it might be a stale message. We still
            # record it but don't apply it.
            if delta.sequence_id <= header.last_sequence_id:
                result = SyncResult(
                    coa_number=delta.coa_number,
                    previous_status=previous_status,
                    new_status=previous_status,
                    applied=False,
                    reason=(
                        f"Out-of-order stale delta (seq {delta.sequence_id} <= "
                        f"{header.last_sequence_id}): illegal transition "
                        f"{previous_status.value} -> {delta.new_status.value}"
                    ),
                )
                self._sync_history.append(result)
                return result

            result = SyncResult(
                coa_number=delta.coa_number,
                previous_status=previous_status,
                new_status=previous_status,
                applied=False,
                reason=(
                    f"Illegal transition: {previous_status.value} -> "
                    f"{delta.new_status.value}"
                ),
            )
            self._sync_history.append(result)
            logger.warning(
                "Illegal transition attempted for %s: %s -> %s",
                delta.coa_number,
                previous_status.value,
                delta.new_status.value,
            )
            return result

        # Apply the valid transition
        now = datetime.utcnow()
        header.current_status = delta.new_status
        header.last_updated = now
        header.processed_keys.add(delta.idempotency_key)

        # Only advance sequence_id if this delta is newer
        if delta.sequence_id > header.last_sequence_id:
            header.last_sequence_id = delta.sequence_id

        result = SyncResult(
            coa_number=delta.coa_number,
            previous_status=previous_status,
            new_status=delta.new_status,
            applied=True,
        )
        self._sync_history.append(result)

        logger.info(
            "Status synced for %s: %s -> %s (source=%s, seq=%d)",
            delta.coa_number,
            previous_status.value,
            delta.new_status.value,
            delta.source,
            delta.sequence_id,
        )
        return result

    def apply_deltas(self, deltas: List[StatusDelta]) -> List[SyncResult]:
        """Apply multiple status deltas in order.

        Args:
            deltas: List of status change events.

        Returns:
            List of SyncResults for each delta.
        """
        return [self.apply_delta(d) for d in deltas]

    def get_header(self, coa_number: str) -> Optional[CoaHeader]:
        """Retrieve the current COA header state."""
        return self._headers.get(coa_number)

    def get_sync_history(
        self, coa_number: Optional[str] = None
    ) -> List[SyncResult]:
        """Retrieve sync history, optionally filtered by COA number."""
        if coa_number is None:
            return list(self._sync_history)
        return [r for r in self._sync_history if r.coa_number == coa_number]

    def reconcile(
        self, external_states: Dict[str, CoaStatus], source: str
    ) -> List[SyncResult]:
        """Scheduled reconciliation: compare external states against local headers.

        For any COA where the external state is ahead of our local state,
        generate and apply synthetic deltas to catch up.

        Args:
            external_states: Map of COA numbers to their externally observed status.
            source: The source system ('JDE' or 'BOTTOMLINE').

        Returns:
            List of SyncResults from reconciliation.
        """
        results: List[SyncResult] = []
        now = datetime.utcnow()

        for coa_number, external_status in external_states.items():
            header = self.get_or_create_header(coa_number)

            if header.current_status == external_status:
                continue

            if header.current_status in TERMINAL_STATUSES:
                continue

            allowed = VALID_TRANSITIONS.get(header.current_status, frozenset())
            if external_status not in allowed:
                continue

            synthetic_delta = StatusDelta(
                coa_number=coa_number,
                new_status=external_status,
                source=source,
                sequence_id=header.last_sequence_id + 1,
                timestamp=now,
                idempotency_key=f"reconcile-{source}-{coa_number}-{now.isoformat()}",
            )
            result = self.apply_delta(synthetic_delta)
            results.append(result)

        return results
