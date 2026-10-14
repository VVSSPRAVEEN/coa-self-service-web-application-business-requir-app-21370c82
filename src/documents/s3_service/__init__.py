"""Scoped, short-expiry presigned URL service for COA distribution (FR7, FR18).

This package owns two public contracts for the FR7/FR18 distribution path:

* ``presigned_url_service`` - the scoped, short-expiry URL surface, which signs
  the request and returns the provenance metadata the audit trail records.
* ``consumption`` - the single-use scope surface, which tracks which issued
  scopes may still be spent so a URL cannot be reused after use.

The print surface (FR8 re-authentication) is owned by the sibling package
``src.documents.print`` and is imported from there, so no module here imports a
name that does not exist on disk and there is exactly one owning workstream per
public model.
"""

from .consumption import (
    SingleUseUrlStore,
    build_strategy_key,
    get_consumption_store,
    reset_consumption_store,
)
from .presigned_url_service import (
    PresignedUrlConfig,
    PresignedUrlError,
    PresignedUrlExpiredError,
    PresignedUrlGenerationError,
    PresignedUrlScopeError,
    PresignedUrlService,
)

__all__ = [
    "PresignedUrlConfig",
    "PresignedUrlError",
    "PresignedUrlExpiredError",
    "PresignedUrlGenerationError",
    "PresignedUrlScopeError",
    "PresignedUrlService",
    "SingleUseUrlStore",
    "build_strategy_key",
    "get_consumption_store",
    "reset_consumption_store",
]
