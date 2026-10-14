"""Sister-plant validation for the COA completion gate (FR9).

Public API of the sister-plant validation subsystem. Every name exported here
is defined by the module it is imported from, so importing the package (which
Python does implicitly for every ``src.validation.sister_plant.<submodule>``
import) can never raise an ImportError.

Ownership of the shared models (FR9): ``.models`` owns the request/result/audit
models, ``.validator`` owns ``SisterPlantValidator`` and ``.service`` owns
``SisterPlantPrintService``. This package module only re-exports them.
"""

from .models import (
    CrossPlantPrintAuditEntry,
    PrintValidationStatus,
    SisterPlantPrintRequest,
    SisterPlantPrintValidationResult,
)
from .service import SisterPlantPrintService
from .validator import SisterPlantValidator

__all__ = [
    "CrossPlantPrintAuditEntry",
    "PrintValidationStatus",
    "SisterPlantPrintRequest",
    "SisterPlantPrintService",
    "SisterPlantPrintValidationResult",
    "SisterPlantValidator",
]
