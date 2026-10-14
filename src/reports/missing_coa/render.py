"""Rendering utilities for missing COA reports."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from src.reports.missing_coa.models import MissingCoaRow

_COLUMNS = [
    "coa_id",
    "vessel_voyage",
    "counterparty",
    "priority",
    "required_ship_date",
    "age_days",
    "blocking_reason",
]


def render_text(rows: Sequence[MissingCoaRow]) -> str:
    """Render the report rows as a formatted plain-text table.

    Args:
        rows: Ordered collection of MissingCoaRow instances.

    Returns:
        A string containing the tabular text representation.
    """
    if not rows:
        return "No missing COA records found.\n"

    # Calculate column widths
    headers = {
        "coa_id": "COA ID",
        "vessel_voyage": "Vessel/Voyage",
        "counterparty": "Counterparty",
        "priority": "Priority",
        "required_ship_date": "Ship Date",
        "age_days": "Age (Days)",
        "blocking_reason": "Blocking Reason",
    }

    col_widths: dict[str, int] = {}
    for key, label in headers.items():
        col_widths[key] = len(label)

    row_dicts = _rows_to_dicts(rows)
    for rd in row_dicts:
        for key in headers:
            val_len = len(str(rd.get(key, "")))
            col_widths[key] = max(col_widths[key], val_len)

    # Build header line
    header_parts = []
    for key in _COLUMNS:
        header_parts.append(headers[key].ljust(col_widths[key]))
    header_line = " | ".join(header_parts)

    separator = "-+-".join("-" * col_widths[k] for k in _COLUMNS)

    # Build data lines
    lines: list[str] = [header_line, separator]
    for rd in row_dicts:
        parts = []
        for key in _COLUMNS:
            parts.append(str(rd.get(key, "")).ljust(col_widths[key]))
        lines.append(" | ".join(parts))

    lines.append("")  # trailing newline
    return "\n".join(lines)


def render_csv(rows: Sequence[MissingCoaRow]) -> str:
    """Render the report rows as CSV content.

    Args:
        rows: Ordered collection of MissingCoaRow instances.

    Returns:
        A string containing the CSV representation.
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    header_labels = [
        "coa_id",
        "vessel_voyage",
        "counterparty",
        "priority",
        "required_ship_date",
        "age_days",
        "blocking_reason",
    ]
    writer.writerow(header_labels)

    row_dicts = _rows_to_dicts(rows)
    for rd in row_dicts:
        writer.writerow([rd.get(col, "") for col in _COLUMNS])

    return output.getvalue()


def _rows_to_dicts(rows: Sequence[MissingCoaRow]) -> list[dict[str, str]]:
    """Convert frozen dataclass rows to flat dictionaries for rendering."""
    result: list[dict[str, str]] = []
    for row in rows:
        ship_date_str = ""
        if row.required_ship_date is not None:
            ship_date_str = row.required_ship_date.isoformat()

        result.append(
            {
                "coa_id": row.coa_id,
                "vessel_voyage": row.vessel_voyage,
                "counterparty": row.counterparty,
                "priority": row.priority or "",
                "required_ship_date": ship_date_str,
                "age_days": str(row.age_days),
                "blocking_reason": row.blocking_reason,
            }
        )
    return result
