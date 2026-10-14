"""Retention policy configuration and evaluation logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# Minimum retention window: 5 years = 1826 days (accounting for leap years)
MINIMUM_RETENTION_DAYS: int = 1826
DEFAULT_RETENTION_YEARS: int = 5


@dataclass(frozen=True)
class RetentionConfig:
    """Configuration for the retention policy engine."""

    retention_days: int = MINIMUM_RETENTION_DAYS
    enable_auto_purge: bool = False
    purge_batch_size: int = 100
    legal_hold_reasons: set[str] = field(
        default_factory=lambda: {
            "litigation",
            "regulatory_investigation",
            "fda_inquiry",
            "customer_dispute",
            "internal_audit",
        }
    )


class RetentionPolicy:
    """Evaluates whether a COA or audit record has met its retention window.

    Enforces a minimum 5-year (1826-day) retention period from the
    record's creation/issuance timestamp.
    """

    def __init__(self, config: RetentionConfig | None = None) -> None:
        self._config = config or RetentionConfig()

    @property
    def config(self) -> RetentionConfig:
        return self._config

    @property
    def retention_days(self) -> int:
        return self._config.retention_days

    def get_expiry_date(self, created_at: datetime) -> datetime:
        """Calculate the earliest date a record may be purged.

        Args:
            created_at: The timestamp when the COA was issued or
                        the audit event was recorded.

        Returns:
            The datetime after which the record is eligible for purge.
        """
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at + timedelta(days=self._config.retention_days)

    def is_expired(self, created_at: datetime, reference_time: datetime | None = None) -> bool:
        """Check if a record has exceeded its mandatory retention window.

        Args:
            created_at: When the record was created.
            reference_time: The time to evaluate against (defaults to now).

        Returns:
            True if the record is past its retention window and eligible
            for purge (assuming no legal hold applies).
        """
        ref = reference_time or datetime.now(UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        expiry = self.get_expiry_date(created_at)
        return ref >= expiry

    def remaining_days(
        self, created_at: datetime, reference_time: datetime | None = None
    ) -> int:
        """Return the number of days remaining in the retention window.

        Returns 0 if already expired.
        """
        ref = reference_time or datetime.now(UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        expiry = self.get_expiry_date(created_at)
        delta = expiry - ref
        return max(0, delta.days)

    def validate_five_year_minimum(self) -> bool:
        """Ensure the configured retention meets the 5-year regulatory minimum."""
        return self._config.retention_days >= MINIMUM_RETENTION_DAYS
