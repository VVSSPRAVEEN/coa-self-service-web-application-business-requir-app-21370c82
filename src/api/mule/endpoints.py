from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status

from src.admin.plant_mapping import (
    PlantMapping,
    PlantMappingUpdate,
    register_plant_mappings,
)

# The contract router is imported first because this module mounts it on ``app``;
# it reads the plant-mapping service dependency through the plant-mapping package
# that is imported immediately below.
from src.api.contract_api import router as contract_router
from src.api.mule.coa_service import COAService, get_coa_service
from src.discovery.search_filters import parse_filters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coa", tags=["COA Search"])

__all__ = [
    "PlantMapping",
    "PlantMappingUpdate",
    "app",
    "contract_router",
    "get_coa_detail",
    "get_coa_status",
    "mount_admin_routes",
    "paginate_results",
    "router",
    "search_coa",
]


def paginate_results(
    results: Sequence[Any],
    *,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Slice ``results`` into the requested page and describe the page envelope.

    Pagination lives here, beside the only endpoint that consumes it, so the
    module never imports a ``src.discovery.listing`` helper that is not part of
    the generated tree. Results may be domain objects or plain mappings; domain
    objects are serialised through their own ``to_dict()`` so the response field
    names stay exactly those of the underlying COA model.
    """
    items: list[Any] = list(results)
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    return {
        "items": [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in page_items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/search")
async def search_coa(
    q: str | None = Query(None, description="COA number or customer search term"),
    plant: str | None = Query(None, description="Plant code filter"),
    start_date: str | None = Query(None, description="Issue date start (YYYY-MM-DD)"),
    end_date: str | None = Query(None, description="Issue date end (YYYY-MM-DD)"),
    status: str | None = Query(None, description="Completion status filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=100, description="Results per page"),
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Search and list COAs through the Mule API layer with filtering and pagination."""
    filters = parse_filters(
        q=q, plant=plant, start_date=start_date, end_date=end_date, status=status
    )
    raw_results = await service.search_coa(filters)
    return paginate_results(raw_results, page=page, page_size=page_size)


@router.get("/status/{coa_number}")
async def get_coa_status(
    coa_number: str,
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Return the completion status of a COA (pending, completed, failed)."""
    coa = await service.get_coa_by_number(coa_number)
    if coa is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "COA_NOT_FOUND", "coa_number": coa_number},
        )
    return {"coa_number": coa_number, "status": coa.status, "completed_at": coa.completed_at}


@router.get("/{coa_number}")
async def get_coa_detail(
    coa_number: str,
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Retrieve the full COA detail view including completion status."""
    coa = await service.get_coa_by_number(coa_number)
    if coa is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "COA_NOT_FOUND", "coa_number": coa_number},
        )
    return coa.to_dict()


def mount_admin_routes(application: FastAPI) -> None:
    """Mount the administrator-managed sister-plant mapping routes (FR9).

    The plant-mapping package owns its models, repository, service and router;
    this module only registers them. ``PlantMapping`` and ``PlantMappingUpdate``
    are imported at module scope from that package, so the names used by the
    sister-plant contract are bound in this module rather than resolved lazily.
    """
    register_plant_mappings(application)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Application lifespan for the Mule-facing COA API service."""
    mount_admin_routes(application)
    yield


# Single configured start target: the Docker CMD and the compose service both run
# `src.api.mule.endpoints:app`, and `app` below is the one and only FastAPI
# instance in this module. Every route declared here is mounted on it.
app = FastAPI(
    title="Ventura COA Mule API",
    version="1.0.0",
    description=(
        "COA search, listing and completion-status API exposed over the Mule "
        "integration layer for the Ventura COA self-service portal."
    ),
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(contract_router)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Liveness/readiness probe served by the container healthcheck."""
    return {"status": "ok", "service": "mule-coa-api"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.mule.endpoints:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
