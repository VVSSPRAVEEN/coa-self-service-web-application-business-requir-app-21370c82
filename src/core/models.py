from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, Field

# Email addresses are validated in-module with an AfterValidator so the shared
# models depend on pydantic and the standard library only. pydantic.EmailStr is
# deliberately not used here: that type requires the optional `email-validator`
# distribution at schema-build time and this backend does not depend on it, so
# importing it would raise ImportError on start-up. EmailAddress is this
# project's own validated-email annotation and is the only email type exported.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _validate_email(value: str) -> str:
    """Return the value unchanged when it is a syntactically valid email address."""
    if not _EMAIL_PATTERN.match(value):
        raise ValueError(f"value is not a valid email address: {value!r}")
    return value


EmailAddress = Annotated[str, AfterValidator(_validate_email)]


class Role(str, Enum):
    """Application roles used for RBAC enforcement."""

    ADMIN = "admin"
    CUSTOMER_SERVICE = "customer_service"
    QA = "qa"
    SHIPPING = "shipping"
    THREE_PL = "3pl"
    END_CUSTOMER = "end_customer"


class OIDCProvider(str, Enum):
    """Supported OIDC identity providers."""

    COGNITO = "cognito"
    AZURE_AD = "azure_ad"


class OIDCToken(BaseModel):
    """Token payload returned from an OIDC provider."""

    access_token: str = Field(..., description="Bearer access token")
    id_token: str = Field(..., description="OIDC ID token")
    token_type: str = "Bearer"
    expires_in: int = Field(..., ge=1)
    refresh_token: Optional[str] = None
    provider: OIDCProvider


class UserClaims(BaseModel):
    """Normalized OIDC user claims after verification."""

    sub: str = Field(..., description="Unique subject identifier")
    email: EmailAddress = Field(...)
    name: Optional[str] = None
    roles: list[Role] = Field(default_factory=list)
    provider: OIDCProvider
    external_account_id: Optional[str] = None
    plant_code: Optional[str] = None
    iss: Optional[str] = None
    aud: Optional[str] = None
    preferred_username: Optional[str] = None

    def has_role(self, role: Role) -> bool:
        return role in self.roles

    def has_any_role(self, roles: list[Role]) -> bool:
        return any(role in self.roles for role in roles)

    def has_all_roles(self, roles: list[Role]) -> bool:
        return all(role in self.roles for role in roles)


class AuditEvent(BaseModel):
    """Immutable audit trail record (5-year retention)."""

    event_id: str = Field(..., description="UUID of the audit event")
    user_sub: str = Field(..., description="Subject of the actor")
    action: str = Field(..., description="Audited action verb")
    resource: str = Field(..., description="Target resource")
    details: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ip_address: Optional[str] = None
    retention_years: int = 5
    outcome: str = "success"  # success, failure, rejected


class AdminUser(BaseModel):
    """External account managed by a Ventura admin."""

    id: str = Field(..., description="Internal admin record ID")
    external_sub: str = Field(..., description="OIDC subject from Cognito/Azure AD")
    email: EmailAddress = Field(...)
    display_name: Optional[str] = None
    provider: OIDCProvider
    roles: list[Role] = Field(default_factory=list)
    plant_code: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None


class CreateUserRequest(BaseModel):
    external_sub: str
    email: EmailAddress
    display_name: Optional[str] = None
    roles: list[Role] = Field(default_factory=list)
    plant_code: Optional[str] = None


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    roles: Optional[list[Role]] = None
    plant_code: Optional[str] = None
    is_active: Optional[bool] = None


class RoleDefinition(BaseModel):
    name: Role
    description: str
    permissions: set[str]
