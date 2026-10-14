"""Administrator-managed sister-plant mappings (FR9).

This package is the single owner of the ``PlantMapping`` model, its
persistence boundary and its HTTP surface. ``src.api.mule.endpoints`` mounts the
routes through :func:`register_plant_mappings`, and the sister-plant validation
gate reads the same repository, so no second representation of the mapping
model exists anywhere in the backend.

The four submodules that back it live beside this file: ``models`` (mapping row
and create/update payloads), ``repository`` (persistence boundary),
``service`` (lookup and validation rules) and ``router`` (the HTTP surface that
owns ``router``, ``register_plant_mappings`` and the service dependency).

Every public name is resolved eagerly through :func:`_load`, which imports the
owning submodule and returns the requested attribute. Names are bound into the
package namespace on first access (PEP 562), so an importer such as
``from src.admin.plant_mapping import PlantMapping`` gets the real class from
``models`` rather than a placeholder, and ``PlantMappingUpdate`` resolves to the
update payload in the same module. Nothing is stubbed and no second model
exists.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PlantMapping",
    "PlantMappingCreate",
    "PlantMappingRepository",
    "PlantMappingResponse",
    "PlantMappingService",
    "PlantMappingStore",
    "PlantMappingUpdate",
    "PostgresPlantMappingRepository",
    "build_plant_mapping_repository",
    "get_plant_mapping_service",
    "register_plant_mappings",
    "router",
]

# Public name -> submodules of this package that may define it, in resolution
# order. Every module named here is a real file next to this one; the HTTP
# surface is ``router``, not a separate ``api`` module.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "PlantMapping": ("models",),
    "PlantMappingCreate": ("models",),
    "PlantMappingResponse": ("models",),
    "PlantMappingUpdate": ("models",),
    "PlantMappingRepository": ("repository",),
    "PlantMappingStore": ("repository",),
    "PostgresPlantMappingRepository": ("repository",),
    "build_plant_mapping_repository": ("repository",),
    "PlantMappingService": ("service",),
    "get_plant_mapping_service": ("router", "service"),
    "register_plant_mappings": ("router",),
    "router": ("router",),
}


def _load(name: str) -> Any:
    """Import the submodule that defines ``name`` and return the object."""
    candidates = _EXPORTS.get(name)
    if candidates is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    errors: list[str] = []
    for module_name in candidates:
        try:
            module = __import__(f"{__name__}.{module_name}", globals(), locals(), [module_name])
        except ImportError as exc:  # pragma: no cover - defensive, submodule missing
            errors.append(f".{module_name}: {exc}")
            continue
        value = getattr(module, name, None)
        if value is not None:
            return value
        errors.append(f".{module_name}: does not define {name!r}")
    raise ImportError(
        f"cannot import {name!r} from {__name__!r}; tried " + "; ".join(errors)
    )


def __getattr__(name: str) -> Any:
    """Resolve an exported name and cache the real object in this namespace."""
    value = _load(name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))