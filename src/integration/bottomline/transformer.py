"""Transform Bottomline flat layout records into internal CostObject/LineItem model."""

from __future__ import annotations

from typing import List

from src.integration.bottomline.models import BottomlineRawRecord, CostObjectLineItem


class TransformationError(Exception):
    """Raised when a record cannot be transformed to the canonical model."""


def transform_to_coa_records(
    raw_records: List[BottomlineRawRecord],
) -> List[CostObjectLineItem]:
    """Transform parsed Bottomline records into canonical CostObjectLineItem models.

    Applies format transformation from Bottomline flat layout to the internal
    CostObject/LineItem domain model used by the COA validation engine.

    Args:
        raw_records: Parsed Bottomline raw records.

    Returns:
        List of canonical CostObjectLineItem instances.

    Raises:
        TransformationError: If a record fails validation during transformation.
    """
    transformed: List[CostObjectLineItem] = []

    for record in raw_records:
        if not record.coa_number:
            raise TransformationError(
                f"Record at line {record.line_number} missing COA number"
            )
        if not record.test_code:
            raise TransformationError(
                f"Record at line {record.line_number} missing test code"
            )

        if (
            record.specification_min is not None
            and record.specification_max is not None
            and record.specification_min > record.specification_max
        ):
            raise TransformationError(
                f"Record at line {record.line_number}: spec_min ({record.specification_min}) "
                f"> spec_max ({record.specification_max})"
            )

        item = CostObjectLineItem(
            coa_number=record.coa_number,
            product_code=record.product_code,
            lot_number=record.lot_number,
            test_code=record.test_code,
            test_result=record.test_result,
            test_unit=record.test_unit,
            specification_min=record.specification_min,
            specification_max=record.specification_max,
            recorded_at=record.recorded_at,
            source_batch_id=record.batch_id,
            source_line_number=record.line_number,
        )
        transformed.append(item)

    return transformed
