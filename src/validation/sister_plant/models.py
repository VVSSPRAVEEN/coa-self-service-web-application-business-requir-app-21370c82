from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PrintValidationStatus(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


class SisterPlantPrintRequest(BaseModel):
    coa_id: str
    requesting_plant_id: str
    target_plant_id: str
    user_id: str
    reason: Optional[str] = None


class SisterPlantPrintValidationResult(BaseModel):
    is_valid: bool
    status: PrintValidationStatus
    message: str
    requesting_plant_id: str
    target_plant_id: str
    coa_id: str


class CrossPlantPrintAuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    coa_id: str
    requesting_plant_id: str
    target_plant_id: str
    user_id: str
    validation_status: PrintValidationStatus
    reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
