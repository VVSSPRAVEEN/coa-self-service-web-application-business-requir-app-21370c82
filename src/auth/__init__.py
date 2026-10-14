"""Authentication surface for the Ventura COA API.

The approved contract authenticates by token exchange rather than by a local
credential store: the client presents the bearer token it already holds from the
OIDC provider and receives the portal's normalised identity back. This package
owns that flow end to end.

:mod:`.oidc` owns every public name: :class:`~src.auth.oidc.User`,
:class:`~src.auth.oidc.AuthResponse`, :func:`~src.auth.oidc.validate_token`,
:func:`~src.auth.oidc.login_flow`, :func:`~src.auth.oidc.get_provider_config`
and the typed :class:`~src.auth.oidc.TokenValidationError`. Role-based access
control is a separate concern and lives in :mod:`src.core.rbac`.
"""

from .oidc import (
    AuthResponse,
    TokenValidationError,
    User,
    get_provider_config,
    login_flow,
    signature_verification_available,
    validate_token,
)

__all__ = [
    "AuthResponse",
    "TokenValidationError",
    "User",
    "get_provider_config",
    "login_flow",
    "signature_verification_available",
    "validate_token",
]
