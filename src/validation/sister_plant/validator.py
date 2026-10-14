from __future__ import annotations

from src.validation.sister_plant.models import (
    CrossPlantPrintAuditEntry,
    PrintValidationStatus,
    SisterPlantPrintRequest,
    SisterPlantPrintValidationResult,
)


class SisterPlantValidator:
    """Validates cross-plant (sister-plant) print requests.

    Ensures that a user at one plant can only print a COA for a sister
    plant when an active plant mapping exists between the two plants.
    Every validation attempt produces an audit entry for compliance.
    """

    def __init__(
        self,
        get_sister_plants_fn: callable[[str], list[str]] | None = None,
    ) -> None:
        self._get_sister_plants = get_sister_plants_fn or (lambda _: [])
        self._audit_log: list[CrossPlantPrintAuditEntry] = []

    def validate_print_request(
        self, request: SisterPlantPrintRequest
    ) -> SisterPlantPrintValidationResult:
        if request.requesting_plant_id == request.target_plant_id:
            result = SisterPlantPrintValidationResult(
                is_valid=True,
                status=PrintValidationStatus.ALLOWED,
                message="Same-plant print; no sister-plant validation required",
                requesting_plant_id=request.requesting_plant_id,
                target_plant_id=request.target_plant_id,
                coa_id=request.coa_id,
            )
            self._record_audit(request, PrintValidationStatus.ALLOWED)
            return result

        sister_plants = self._get_sister_plants(request.requesting_plant_id)
        if request.target_plant_id in sister_plants:
            result = SisterPlantPrintValidationResult(
                is_valid=True,
                status=PrintValidationStatus.ALLOWED,
                message="Cross-plant print allowed: valid sister-plant mapping exists",
                requesting_plant_id=request.requesting_plant_id,
                target_plant_id=request.target_plant_id,
                coa_id=request.coa_id,
            )
            self._record_audit(request, PrintValidationStatus.ALLOWED)
            return result

        result = SisterPlantPrintValidationResult(
            is_valid=False,
            status=PrintValidationStatus.DENIED,
            message=(
                f"Cross-plant print denied: no active mapping from "
                f"'{request.requesting_plant_id}' to '{request.target_plant_id}'"
            ),
            requesting_plant_id=request.requesting_plant_id,
            target_plant_id=request.target_plant_id,
            coa_id=request.coa_id,
        )
        self._record_audit(request, PrintValidationStatus.DENIED)
        return result

    def _record_audit(
        self, request: SisterPlantPrintRequest, status: PrintValidationStatus
    ) -> None:
        entry = CrossPlantPrintAuditEntry(
            coa_id=request.coa_id,
            requesting_plant_id=request.requesting_plant_id,
            target_plant_id=request.target_plant_id,
            user_id=request.user_id,
            validation_status=status,
            reason=request.reason,
        )
        self._audit_log.append(entry)

    def get_audit_log(self) -> list[CrossPlantPrintAuditEntry]:
        return list(self._audit_log)

    def clear_audit_log(self) -> None:
        self._audit_log.clear()
