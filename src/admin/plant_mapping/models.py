from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class PlantMapping(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_plant_id: str
    sister_plant_id: str
    is_active: bool = True
    created_by: str
    updated_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlantMappingCreate(BaseModel):
    source_plant_id: str
    sister_plant_id: str
    created_by: str


class PlantMappingUpdate(BaseModel):
    sister_plant_id: str | None = None
    is_active: bool | None = None
    updated_by: str


class PlantMappingResponse(BaseModel):
    id: str
    source_plant_id: str
    sister_plant_id: str
    is_active: bool
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
