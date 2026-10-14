"""Lookup and validation rules for administrator-managed sister-plant mappings (FR9).

The mapping row and its create/update payloads are owned by
:mod:`src.admin.plant_mapping.models`, and the persistence boundary is owned by
:mod:`src.admin.plant_mapping.repository`. This module imports both and adds the
domain rules on top; it deliberately declares no model of its own, so there is
exactly one representation of ``PlantMapping`` in the backend and the router,
the repository and the unit tests all exchange the same objects.

Failures are typed rather than HTTP-shaped: :class:`DuplicateMappingError` and
:class:`MappingNotFoundError` describe *why* a mapping could not be created,
read, updated or deleted. :mod:`src.admin.plant_mapping.router` is the only
place that maps them onto status codes (409 and 404).
"""

from __future__ import annotations

from src.admin.plant_mapping.models import (
    PlantMapping,
    PlantMappingCreate,
    PlantMappingResponse,
    PlantMappingUpdate,
)
from src.admin.plant_mapping.repository import (
    PlantMappingRepository,
    PlantMappingStore,
    build_plant_mapping_repository,
)

__all__ = [
    "DuplicateMappingError",
    "MappingNotFoundError",
    "PlantMapping",
    "PlantMappingCreate",
    "PlantMappingRepository",
    "PlantMappingResponse",
    "PlantMappingService",
    "PlantMappingStore",
    "PlantMappingUpdate",
]


class DuplicateMappingError(Exception):
    """Raised when an active mapping already exists for the given source and sister plant."""


class MappingNotFoundError(Exception):
    """Raised when a requested plant mapping does not exist."""


class PlantMappingService:
    """Domain rules layered over a :class:`PlantMappingStore`.

    The store is chosen by :func:`build_plant_mapping_repository`, so a deployment
    that configures a DSN gets the durable PostgreSQL store and a test that
    configures none gets the in-memory fallback. Either way the rules below are
    identical, which is why no ``if postgres`` branch appears in this module.
    """

    def __init__(self, repository: PlantMappingStore | None = None) -> None:
        self._repo = repository or build_plant_mapping_repository()

    async def create_mapping(
        self, payload: PlantMappingCreate
    ) -> PlantMappingResponse:
        existing = await self._repo.get_by_source_plant(payload.source_plant_id)
        for mapping in existing:
            if mapping.sister_plant_id == payload.sister_plant_id and mapping.is_active:
                raise DuplicateMappingError(
                    "Active mapping already exists for this source and sister plant combination"
                )
        created = await self._repo.create(payload)
        return PlantMappingResponse(**created.model_dump())

    async def get_mapping(self, mapping_id: str) -> PlantMappingResponse:
        mapping = await self._repo.get_by_id(mapping_id)
        if mapping is None:
            raise MappingNotFoundError("Plant mapping not found")
        return PlantMappingResponse(**mapping.model_dump())

    async def list_mappings(self) -> list[PlantMappingResponse]:
        mappings = await self._repo.list_all()
        return [PlantMappingResponse(**m.model_dump()) for m in mappings]

    async def list_by_source_plant(
        self, source_plant_id: str
    ) -> list[PlantMappingResponse]:
        mappings = await self._repo.get_by_source_plant(source_plant_id)
        return [PlantMappingResponse(**m.model_dump()) for m in mappings]

    async def update_mapping(
        self, mapping_id: str, payload: PlantMappingUpdate
    ) -> PlantMappingResponse:
        updated = await self._repo.update(mapping_id, payload)
        if updated is None:
            raise MappingNotFoundError("Plant mapping not found")
        return PlantMappingResponse(**updated.model_dump())

    async def delete_mapping(self, mapping_id: str) -> None:
        deleted = await self._repo.delete(mapping_id)
        if not deleted:
            raise MappingNotFoundError("Plant mapping not found")

    async def get_active_sister_plants(self, source_plant_id: str) -> list[str]:
        mappings = await self._repo.get_by_source_plant(source_plant_id)
        return [m.sister_plant_id for m in mappings if m.is_active]