from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException, status

from src.validation.sister_plant.models import (
    CrossPlantPrintAuditEntry,
    SisterPlantPrintRequest,
    SisterPlantPrintValidationResult,
)
from src.validation.sister_plant.validator import SisterPlantValidator

# Signature of the injected sister-plant lookup: plant id -> mapped plant ids.
GetSisterPlantsFn = Callable[[str], list[str]]


class SisterPlantPrintService:
    """High-level service that wraps validation and exposes it to the router."""

    def __init__(
        self,
        validator: SisterPlantValidator | None = None,
        get_sister_plants_fn: GetSisterPlantsFn | None = None,
    ) -> None:
        self._validator = validator or SisterPlantValidator(
            get_sister_plants_fn=get_sister_plants_fn
        )

    async def validate_and_audit(
        self, request: SisterPlantPrintRequest
    ) -> SisterPlantPrintValidationResult:
        result = self._validator.validate_print_request(request)
        if not result.is_valid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=result.message,
            )
        return result

    async def get_audit_log(self) -> list[CrossPlantPrintAuditEntry]:
        return self._validator.get_audit_log()
