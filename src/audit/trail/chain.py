"""Hash-chain logic for tamper-evident audit entries."""

from __future__ import annotations

import hashlib

from src.audit.trail.models import AuditLogEntry


class AuditChain:
    """Manages the hash chain for audit log entries.

    Provides methods to compute entry hashes and verify the
    integrity of an ordered sequence of entries.
    """

    @staticmethod
    def compute_hash(entry: AuditLogEntry) -> str:
        """Compute the SHA-256 hash of an entry's canonical payload."""
        payload = entry.compute_canonical_payload()
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def seal_entry(
        entry: AuditLogEntry,
        previous_hash: str,
        sequence_number: int,
    ) -> AuditLogEntry:
        """Create a new sealed entry linked to the previous hash.

        Returns a new AuditLogEntry with previous_hash, sequence_number,
        and entry_hash populated.
        """
        # Rebuild with updated chain fields (model is frozen)
        updated = AuditLogEntry(
            event_id=entry.event_id,
            event_type=entry.event_type,
            user_sub=entry.user_sub,
            resource_id=entry.resource_id,
            details=entry.details,
            ip_address=entry.ip_address,
            timestamp=entry.timestamp,
            previous_hash=previous_hash,
            sequence_number=sequence_number,
            entry_hash="",  # placeholder before computing
        )
        entry_hash = AuditChain.compute_hash(updated)
        sealed = AuditLogEntry(
            event_id=updated.event_id,
            event_type=updated.event_type,
            user_sub=updated.user_sub,
            resource_id=updated.resource_id,
            details=updated.details,
            ip_address=updated.ip_address,
            timestamp=updated.timestamp,
            previous_hash=updated.previous_hash,
            sequence_number=updated.sequence_number,
            entry_hash=entry_hash,
        )
        return sealed

    @staticmethod
    def verify_integrity(entries: list[AuditLogEntry]) -> tuple[bool, int | None]:
        """Verify the integrity of an ordered list of audit entries.

        Checks:
        1. Sequence numbers are strictly monotonically increasing by 1.
        2. Each entry's previous_hash matches the prior entry's entry_hash.
        3. Each entry's entry_hash matches its recomputed hash.

        Returns:
            A tuple of (is_valid, failed_index). If valid, failed_index is None.
        """
        if not entries:
            return True, None

        expected_prev_hash = "GENESIS"

        for idx, entry in enumerate(entries):
            # Check sequence ordering
            if entry.sequence_number != idx:
                return False, idx

            # Check chain linkage
            if entry.previous_hash != expected_prev_hash:
                return False, idx

            # Verify hash integrity
            recomputed = AuditChain.compute_hash(entry)
            if recomputed != entry.entry_hash:
                return False, idx

            expected_prev_hash = entry.entry_hash

        return True, None

    @staticmethod
    def detect_tampering(
        original_entries: list[AuditLogEntry],
        suspect_entries: list[AuditLogEntry],
    ) -> list[int]:
        """Compare two chains and return indices where entries differ.

        Useful for detecting mutations or reordering between a trusted
        snapshot and current state.
        """
        tampered_indices: list[int] = []
        max_len = max(len(original_entries), len(suspect_entries))

        for idx in range(max_len):
            if idx >= len(original_entries) or idx >= len(suspect_entries):
                tampered_indices.append(idx)
                continue
            orig = original_entries[idx]
            suspect = suspect_entries[idx]
            if orig.model_dump() != suspect.model_dump():
                tampered_indices.append(idx)

        return tampered_indices
