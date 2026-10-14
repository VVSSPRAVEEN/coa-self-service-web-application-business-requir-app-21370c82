from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class PlantMapping(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_plant_id: str
    sister_plant_id: str
    is_active: bool = True
    created_by: str
    updated_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlantMappingCreate(BaseModel):
    source_plant_id: str
    sister_plant_id: str
    created_by: str


class PlantMappingUpdate(BaseModel):
    sister_plant_id: Optional[str] = None
    is_active: Optional[bool] = None
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
