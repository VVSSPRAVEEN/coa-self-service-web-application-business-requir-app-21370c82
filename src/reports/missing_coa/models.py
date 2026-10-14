"""Data models for the missing COA report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class MissingCoaRow:
    """Represents a single missing, incomplete, or overdue COA record."""

    coa_id: str
    vessel: str
    voyage: str
    counterparty: str
    priority: str | None
    required_ship_date: date | None
    age_days: int
    blocking_reason: str

    @property
    def vessel_voyage(self) -> str:
        """Return combined vessel/voyage string."""
        if self.voyage:
            return f"{self.vessel}/{self.voyage}"
        return self.vessel


@dataclass(frozen=True)
class ReportResult:
    """Container for generated report data."""

    rows: tuple[MissingCoaRow, ...]
    text_body: str
    csv_body: str
    generated_at: str
    report_window: str
