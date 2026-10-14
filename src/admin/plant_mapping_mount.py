"""Backward-compatible re-export of the plant-mapping route registration (FR9).

The plant-mapping package owns its models, repository, service and HTTP surface,
and :func:`src.admin.plant_mapping.router.register_plant_mappings` is the real
registration function: it mounts the package's own ``APIRouter`` onto the
application, exactly as ``mount_admin_routes`` in
:mod:`src.api.mule.endpoints` documents. This module only forwards to that owner
so historical import paths keep resolving.

It used to bind the symbol onto the router module from the outside at import
time, which meant the application only imported because of a monkey-patch, and
it used to expose ``ensure_plant_mapping_mount`` as a "call me at import time"
hook. Both are gone. The owner defines the function, the package re-exports it
through :pep:`562` module ``__getattr__``, and ``src.api.mule.endpoints`` calls
it once from ``mount_admin_routes``. There is no side effect left for a
compatibility entry point to perform, so there is no such entry point.
"""

from __future__ import annotations

from src.admin.plant_mapping.router import register_plant_mappings

__all__ = ["register_plant_mappings"]
