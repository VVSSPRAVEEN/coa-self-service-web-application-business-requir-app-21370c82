"""Parser for Bottomline flat file layout into canonical records."""

from __future__ import annotations

from datetime import datetime
from typing import List

from src.integration.bottomline.models import BottomlineRawRecord


class BottomlineParseError(Exception):
    """Raised when a Bottomline flat file cannot be parsed."""

    def __init__(self, message: str, line_number: int | None = None) -> None:
        self.line_number = line_number
        super().__init__(message)


def parse_bottomline_flat(content: str, batch_id: str) -> List[BottomlineRawRecord]:
    """Parse Bottomline flat layout content into raw records.

    The Bottomline flat file format uses pipe-delimited fields:
    COA_NUMBER|PRODUCT_CODE|LOT_NUMBER|TEST_CODE|TEST_RESULT|TEST_UNIT|SPEC_MIN|SPEC_MAX|RECORDED_AT

    Args:
        content: Raw text content of the Bottomline file.
        batch_id: Identifier for the batch being ingested.

    Returns:
        List of parsed BottomlineRawRecord instances.

    Raises:
        BottomlineParseError: If any line cannot be parsed correctly.
    """
    records: List[BottomlineRawRecord] = []
    lines = content.strip().splitlines()

    if not lines:
        raise BottomlineParseError("Empty file content", line_number=0)

    for line_idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split("|")
        if len(parts) != 9:
            raise BottomlineParseError(
                f"Expected 9 pipe-delimited fields, got {len(parts)}",
                line_number=line_idx,
            )

        coa_number = parts[0].strip()
        product_code = parts[1].strip()
        lot_number = parts[2].strip()
        test_code = parts[3].strip()
        test_result = parts[4].strip()
        test_unit = parts[5].strip()
        spec_min_str = parts[6].strip()
        spec_max_str = parts[7].strip()
        recorded_at_str = parts[8].strip()

        if not coa_number:
            raise BottomlineParseError("Missing COA number", line_number=line_idx)
        if not test_code:
            raise BottomlineParseError("Missing test code", line_number=line_idx)

        spec_min: float | None = None
        spec_max: float | None = None

        if spec_min_str:
            try:
                spec_min = float(spec_min_str)
            except ValueError as exc:
                raise BottomlineParseError(
                    f"Invalid spec_min value: {spec_min_str}", line_number=line_idx
                ) from exc

        if spec_max_str:
            try:
                spec_max = float(spec_max_str)
            except ValueError as exc:
                raise BottomlineParseError(
                    f"Invalid spec_max value: {spec_max_str}", line_number=line_idx
                ) from exc

        try:
            recorded_at = datetime.fromisoformat(recorded_at_str)
        except ValueError as exc:
            raise BottomlineParseError(
                f"Invalid datetime format: {recorded_at_str}", line_number=line_idx
            ) from exc

        records.append(
            BottomlineRawRecord(
                batch_id=batch_id,
                coa_number=coa_number,
                product_code=product_code,
                lot_number=lot_number,
                test_code=test_code,
                test_result=test_result,
                test_unit=test_unit,
                specification_min=spec_min,
                specification_max=spec_max,
                recorded_at=recorded_at,
                raw_line=raw_line,
                line_number=line_idx,
            )
        )

    if not records:
        raise BottomlineParseError("No valid records found in file", line_number=0)

    return records
