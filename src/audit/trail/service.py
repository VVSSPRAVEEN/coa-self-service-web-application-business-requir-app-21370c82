"""Core service for recording and verifying audit trail events."""

from __future__ import annotations

from src.audit.trail.chain import AuditChain
from src.audit.trail.models import AuditEventType, AuditLogEntry
from src.audit.trail.repository import AuditRepository


class AuditTrailService:
    """High-level service for the immutable audit trail.

    Coordinates chain sealing, persistence, and integrity verification.
    """

    def __init__(self, repository: AuditRepository | None = None) -> None:
        self._repository = repository or AuditRepository()
        self._chain = AuditChain()

    def record_event(
        self,
        event_type: AuditEventType,
        user_sub: str,
        resource_id: str,
        details: str | None = None,
        ip_address: str | None = None,
        idempotency_key: str | None = None,
    ) -> AuditLogEntry:
        """Record a new audit event, sealing it into the hash chain.

        If idempotency_key is provided and matches an existing event_id,
        the existing entry is returned without creating a duplicate.
        """
        # Idempotency check
        if idempotency_key is not None:
            existing = self._repository.get_all()
            for entry in existing:
                if entry.event_id == idempotency_key:
                    return entry

        current_count = self._repository.count()
        entries = self._repository.get_all()

        previous_hash = "GENESIS"
        if entries:
            previous_hash = entries[-1].entry_hash

        raw_entry = AuditLogEntry(
            event_id=idempotency_key if idempotency_key else AuditLogEntry.model_fields["event_id"].default_factory(),  # type: ignore[misc]
            event_type=event_type,
            user_sub=user_sub,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
        )

        sealed_entry = self._chain.seal_entry(
            entry=raw_entry,
            previous_hash=previous_hash,
            sequence_number=current_count,
        )

        self._repository.append(sealed_entry)
        return sealed_entry

    def verify_chain_integrity(self) -> tuple[bool, int | None]:
        """Verify the entire audit chain has not been tampered with.

        Returns:
            Tuple of (is_valid, failed_index).
        """
        entries = self._repository.get_all()
        return self._chain.verify_integrity(entries)

    def get_events_for_resource(self, resource_id: str) -> list[AuditLogEntry]:
        """Retrieve all audit events for a specific COA/resource."""
        return self._repository.get_by_resource(resource_id)

    def get_events_by_type(self, event_type: AuditEventType) -> list[AuditLogEntry]:
        """Retrieve all audit events of a given type."""
        return self._repository.get_by_type(event_type)

    def get_all_events(self) -> list[AuditLogEntry]:
        """Retrieve all audit events in chain order."""
        return self._repository.get_all()

    def detect_tampering(
        self, trusted_snapshot: list[AuditLogEntry]
    ) -> list[int]:
        """Compare a trusted snapshot against current chain state.

        Returns list of indices where entries differ.
        """
        current = self._repository.get_all()
        return self._chain.detect_tampering(trusted_snapshot, current)