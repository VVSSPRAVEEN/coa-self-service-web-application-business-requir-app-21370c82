"""HTTP surface for administrator-managed sister-plant mappings (FR9).

This module owns ``router``, ``register_plant_mappings`` and the service
dependency. The request/response payloads come from
:mod:`src.admin.plant_mapping.models` and the typed domain errors come from
:mod:`src.admin.plant_mapping.service`, so the wire contract and the domain
rules are expressed exactly once each.

The router is the only place that turns a domain failure into a status code:
:class:`~src.admin.plant_mapping.service.DuplicateMappingError` becomes 409 and
:class:`~src.admin.plant_mapping.service.MappingNotFoundError` becomes 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from src.admin.plant_mapping.models import (
    PlantMappingCreate,
    PlantMappingResponse,
    PlantMappingUpdate,
)
from src.admin.plant_mapping.service import (
    DuplicateMappingError,
    MappingNotFoundError,
    PlantMappingService,
)

router = APIRouter(prefix="/admin/plant-mappings", tags=["Plant Mapping Admin"])

__all__ = ["get_plant_mapping_service", "register_plant_mappings", "router"]

_service_instance: PlantMappingService | None = None


def get_plant_mapping_service() -> PlantMappingService:
    """Return the process-wide mapping service, creating it on first use."""
    global _service_instance
    if _service_instance is None:
        _service_instance = PlantMappingService()
    return _service_instance


def register_plant_mappings(application: FastAPI) -> None:
    """Mount the administrator-managed sister-plant mapping routes (FR9).

    Args:
        application: The FastAPI application the routes are mounted on.
    """
    application.include_router(router)

SERVICE_DEPENDENCY = Depends(get_plant_mapping_service)
@router.post("", response_model=PlantMappingResponse, status_code=201)
async def create_plant_mapping(
    payload: PlantMappingCreate,
    service: PlantMappingService = SERVICE_DEPENDENCY,
) -> PlantMappingResponse:
    try:
        return await service.create_mapping(payload)
    except DuplicateMappingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("", response_model=list[PlantMappingResponse])
async def list_plant_mappings(
    service: PlantMappingService = SERVICE_DEPENDENCY,
) -> list[PlantMappingResponse]:
    return await service.list_mappings()


@router.get("/by-source/{source_plant_id}", response_model=list[PlantMappingResponse])
async def list_by_source_plant(
    source_plant_id: str,
    service: PlantMappingService = SERVICE_DEPENDENCY,
) -> list[PlantMappingResponse]:
    return await service.list_by_source_plant(source_plant_id)


@router.get("/{mapping_id}", response_model=PlantMappingResponse)
async def get_plant_mapping(
    mapping_id: str,
    service: PlantMappingService = Depends(get_plant_mapping_service),  # noqa: B008
) -> PlantMappingResponse:
    try:
        return await service.get_mapping(mapping_id)
    except MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{mapping_id}", response_model=PlantMappingResponse)
async def update_plant_mapping(
    mapping_id: str,
    payload: PlantMappingUpdate,
    service: PlantMappingService = Depends(get_plant_mapping_service),  # noqa: B008
) -> PlantMappingResponse:
    try:
        return await service.update_mapping(mapping_id, payload)
    except MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{mapping_id}", status_code=204)
async def delete_plant_mapping(
    mapping_id: str,
    service: PlantMappingService = Depends(get_plant_mapping_service),  # noqa: B008
) -> None:
    try:
        await service.delete_mapping(mapping_id)
    except MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
