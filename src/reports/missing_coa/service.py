"""Service layer for generating missing COA reports."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from src.reports.missing_coa.models import MissingCoaRow, ReportResult
from src.reports.missing_coa.render import render_csv, render_text

logger = logging.getLogger(__name__)


class DiscoveryRepository(Protocol):
    """Protocol for querying discovered COA records."""

    def fetch_missing_coas(self) -> List[Dict[str, Any]]:
        """Return raw discovery records that are missing or incomplete."""
        ...


class ValidationRepository(Protocol):
    """Protocol for querying validation results."""

    def fetch_failed_validations(self) -> List[Dict[str, Any]]:
        """Return raw records that failed validation."""
        ...


class MissingCoaReportService:
    """Generates deterministic, sortable missing-COA reports."""

    def __init__(
        self,
        discovery_repo: Optional[DiscoveryRepository] = None,
        validation_repo: Optional[ValidationRepository] = None,
        clock: Optional[Callable[[], date]] = None,
    ) -> None:
        self._discovery_repo = discovery_repo
        self._validation_repo = validation_repo
        self._clock = clock or (lambda: datetime.now(timezone.utc).date())

    def generate_report(
        self,
        raw_records: Optional[List[Dict[str, Any]]] = None,
        report_window: str = "daily",
    ) -> ReportResult:
        """Build the missing COA report from repository data or injected records.

        Args:
            raw_records: Optional pre-fetched records. If None, queries repositories.
            report_window: Identifier for the reporting window (used for idempotency).

        Returns:
            A frozen ReportResult containing sorted rows and rendered bodies.
        """
        today = self._clock()
        records = raw_records if raw_records is not None else self._fetch_all_records()

        rows: List[MissingCoaRow] = []
        for record in records:
            row = self._build_row(record, today)
            if row is not None:
                rows.append(row)

        # Deterministic sort: by age_days descending, then coa_id ascending
        sorted_rows = sorted(rows, key=lambda r: (-r.age_days, r.coa_id))
        frozen_rows = tuple(sorted_rows)

        now_str = datetime.now(timezone.utc).isoformat()
        text_body = render_text(frozen_rows)
        csv_body = render_csv(frozen_rows)

        logger.info(
            "Generated missing COA report",
            extra={
                "row_count": len(frozen_rows),
                "report_window": report_window,
                "generated_at": now_str,
            },
        )

        return ReportResult(
            rows=frozen_rows,
            text_body=text_body,
            csv_body=csv_body,
            generated_at=now_str,
            report_window=report_window,
        )

    def _fetch_all_records(self) -> List[Dict[str, Any]]:
        """Fetch records from both discovery and validation repositories."""
        records: List[Dict[str, Any]] = []
        if self._discovery_repo is not None:
            try:
                records.extend(self._discovery_repo.fetch_missing_coas())
            except Exception:
                logger.exception("Failed to fetch missing COAs from discovery repo")
        if self._validation_repo is not None:
            try:
                records.extend(self._validation_repo.fetch_failed_validations())
            except Exception:
                logger.exception("Failed to fetch failed validations from validation repo")
        return records

    def _build_row(self, record: Dict[str, Any], today: date) -> Optional[MissingCoaRow]:
        """Convert a raw record dict into a MissingCoaRow if it qualifies."""
        coa_id = str(record.get("coa_id", ""))
        if not coa_id:
            return None

        vessel = str(record.get("vessel", ""))
        voyage = str(record.get("voyage", ""))
        counterparty = str(record.get("counterparty", ""))
        priority_raw = record.get("priority")
        priority = str(priority_raw) if priority_raw is not None else None
        ship_date_raw = record.get("required_ship_date")

        required_ship_date: Optional[date] = None
        if isinstance(ship_date_raw, date):
            required_ship_date = ship_date_raw
        elif isinstance(ship_date_raw, str) and ship_date_raw:
            try:
                required_ship_date = date.fromisoformat(ship_date_raw)
            except ValueError:
                required_ship_date = None

        age_days = 0
        if required_ship_date is not None:
            age_days = (today - required_ship_date).days
            if age_days < 0:
                age_days = 0

        blocking_reason = self._determine_blocking_reason(
            record, required_ship_date, priority, age_days
        )

        if blocking_reason == "none":
            return None

        return MissingCoaRow(
            coa_id=coa_id,
            vessel=vessel,
            voyage=voyage,
            counterparty=counterparty,
            priority=priority,
            required_ship_date=required_ship_date,
            age_days=age_days,
            blocking_reason=blocking_reason,
        )

    @staticmethod
    def _determine_blocking_reason(
        record: Dict[str, Any],
        required_ship_date: Optional[date],
        priority: Optional[str],
        age_days: int,
    ) -> str:
        """Determine why this COA is blocking."""
        reasons: List[str] = []

        status = str(record.get("status", "")).lower()
        if status in ("missing", "absent", ""):
            reasons.append("COA absent")

        if required_ship_date is None:
            reasons.append("missing ship date")

        if priority is None or priority.strip() == "":
            reasons.append("missing priority")

        validation_passed = record.get("validation_passed")
        if validation_passed is False or str(validation_passed).lower() == "false":
            reasons.append("failed validation")

        if required_ship_date is not None and age_days > 0:
            reasons.append(f"overdue by {age_days} days")

        if not reasons:
            # Check if explicitly flagged as incomplete
            is_incomplete = record.get("incomplete", False)
            if is_incomplete:
                reasons.append("incomplete record")
            else:
                return "none"

        return "; ".join(reasons)
