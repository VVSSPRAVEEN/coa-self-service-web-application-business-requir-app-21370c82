from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends

from src.validation.sister_plant.models import (
    CrossPlantPrintAuditEntry,
    SisterPlantPrintRequest,
    SisterPlantPrintValidationResult,
)
from src.validation.sister_plant.service import SisterPlantPrintService

router = APIRouter(prefix="/validation/sister-plant", tags=["Sister Plant Validation"])

_service_instance: SisterPlantPrintService | None = None


def get_sister_plant_service() -> SisterPlantPrintService:
    global _service_instance
    if _service_instance is None:
        _service_instance = SisterPlantPrintService()
    return _service_instance


@router.post("/validate-print", response_model=SisterPlantPrintValidationResult)
async def validate_cross_plant_print(
    request: SisterPlantPrintRequest,
    service: SisterPlantPrintService = Depends(get_sister_plant_service),  # noqa: B008
) -> SisterPlantPrintValidationResult:
    return await service.validate_and_audit(request)


@router.get("/audit-log", response_model=List[CrossPlantPrintAuditEntry])
async def get_print_audit_log(
    service: SisterPlantPrintService = Depends(get_sister_plant_service),  # noqa: B008
) -> List[CrossPlantPrintAuditEntry]:
    return await service.get_audit_log()
