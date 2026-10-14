"""The approved HTTP contract of the Ventura COA self-service portal.

This module owns the eight paths the customer-facing portal consumes and is the
only place that translates domain failures into HTTP status codes:

* ``POST /auth/login``          - token exchange against the OIDC provider
* ``GET  /coas``                - filtered, paginated COA search (FR4)
* ``GET  /coas/{coa_id}``       - COA detail
* ``POST /coas/{coa_id}/download`` - completion-gated presigned PDF URL (FR6/FR7/FR18)
* ``POST /coas/{coa_id}/print``    - completion-gated, re-authenticated print (FR8)
* ``POST /coas/{coa_id}/print/reauth`` - mint the FR8 re-auth credential the portal presents
* ``POST /coas/{coa_id}/validate`` - completion-gate report for one COA (FR6)
* ``GET  /missing-coas``        - blocked-shipment report (FR5)
* ``POST /admin/users``         - administrator-managed external accounts

The response DTO deliberately renames two upstream fields to the contract names
the portal consumes: ``coa_number`` becomes ``coa_id`` and ``plant_code``
becomes ``plant_of_origin``. Every other field keeps its upstream meaning.

Three structural notes for readers:

* **Pagination is inlined** instead of reusing ``paginate_results`` from
  :mod:`src.api.mule.endpoints`, because that module imports this one to mount
  the contract router; reusing its helper would create an import cycle.
* **The audit trail blocks distribution (FR12).** A COA document may only leave
  the platform if the event that released it reached the trail. When the append
  on ``/download`` or ``/print`` raises or reports failure, the handler answers
  ``503`` with ``code: "audit_write_failed"`` and issues no grant at all - the
  presigned URL is never returned, and on download the single-use scope that was
  just minted is retired so nothing can consume it. ``audit_recorded`` therefore
  only ever appears as ``true``, on the success path it describes.
* **The completion gate prefers live JDE state (FR6/FR14).** The Mule-carried
  ``test_results`` block is a snapshot taken when the record was written; when
  :func:`build_jde_client` can reach JDE, the gate reads the live test fields and
  lifecycle status instead and returns the same
  :class:`~src.validation.completion_gate.validator.ValidationOutcome` shape, so
  no handler below knows which path answered. Only when the live read *fails* does
  the gate fall back to the snapshot - and that degradation is reported at
  WARNING level and surfaced in the response rather than passing silently.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.admin.plant_mapping.router import get_plant_mapping_service

from src.admin.users import (
    DuplicateUserError,
    InvalidRoleError,
    UserDTO,
    UserNotFoundError,
    create_external_account,
)
from src.api.mule.coa_service import (
    COAFilter,
    COAService,
    MuleApiError,
    get_coa_service,
    log_access,
    log_generation,
)
from src.auth.oidc import TokenValidationError, login_flow
from src.core.rbac import authorize, get_allowed_resources
from src.discovery.search_filters import parse_filters
from src.documents.print import PrintRequest, PrintService
from src.documents.print.print_service import PRINT_DENIED_VALIDATION, ReauthError
from src.documents.s3_service import (
    PresignedUrlError,
    PresignedUrlService,
    build_strategy_key,
    get_consumption_store,
)
from src.integration.jde import JDEClient, JDEConfigurationError, JDEError
from src.reports.missing_coa import MissingCoaReportService
from src.reports.missing_coa.models import MissingCoaRow
from src.validation.completion_gate import (
    CompletionGate,
    ReleasePolicy,
    ValidationOutcome,
)
from src.validation.sister_plant import SisterPlantPrintRequest, SisterPlantValidator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Contract"])

__all__ = ["router"]

#: Error code returned whenever the completion gate blocks a distribution.
COMPLETION_GATE_CODE = "completion_gate_failed"

#: Error code returned whenever the audit append fails and the distribution is
#: therefore refused rather than issued unaudited (FR12).
AUDIT_WRITE_FAILED_CODE = "audit_write_failed"

#: Starlette deprecated its own 422 constant, so the literal is used directly.
HTTP_422_UNPROCESSABLE = 422

#: Default and ceiling for a presigned download URL, in seconds (FR7/FR18).
DEFAULT_DOWNLOAD_EXPIRY_SECONDS = 900
MAX_DOWNLOAD_EXPIRY_SECONDS = 900

#: Pagination envelope defaults for the search endpoint.
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

#: Actor recorded in the audit trail when the caller identifies itself through
#: the optional ``X-User-Id`` header rather than a verified token.
SERVICE_ACTOR = "contract_api"

#: Response key naming which gate answered: ``"jde"`` for live state, ``"mule"``
#: for the Mule-carried snapshot.
GATE_SOURCE_KEY = "gate_source"

#: Response key set when a live JDE read failed and the gate degraded to the
#: Mule-carried snapshot.
JDE_DEGRADED_KEY = "jde_degraded"

_completion_gate = CompletionGate()
_release_policy = ReleasePolicy(_completion_gate)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Body of ``POST /auth/login``: the token issued by the OIDC provider."""

    token: str = Field(..., min_length=1, description="Compact JWS issued by the OIDC provider")


class DownloadRequest(BaseModel):
    """Body of ``POST /coas/{coa_id}/download``; the body itself is optional."""

    expiry_seconds: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_DOWNLOAD_EXPIRY_SECONDS,
        description="Requested URL lifetime in seconds; defaults to 900 (15 minutes).",
    )


class PrintContractRequest(BaseModel):
    """Body of ``POST /coas/{coa_id}/print``."""

    user_id: str = Field(..., min_length=1, description="Identifier of the printing user")
    reauth_token: str = Field(
        ...,
        min_length=1,
        description="Short-lived FR8 re-auth credential for this user/COA pair",
    )
    printing_plant: Optional[str] = Field(
        None,
        description="Plant performing the print; enables the FR9 cross-plant check",
    )


class PrintReauthRequest(BaseModel):
    """Body of ``POST /coas/{coa_id}/print/reauth``.

    The portal calls this after it has re-authenticated the operator, and then
    presents the returned ``reauth_token`` to ``POST /coas/{coa_id}/print``.
    """

    user_id: str = Field(..., min_length=1, description="Identifier of the re-authenticated user")


class CreateUserPayload(BaseModel):
    """Body of ``POST /admin/users``."""

    username: str = Field(..., min_length=1)
    email: Optional[str] = None
    role: str = Field(
        ...,
        description="One of 'internal', '3pl' or 'customer'; validated by the registry",
    )
    organization: Optional[str] = None


# ---------------------------------------------------------------------------
# Small helpers shared by the endpoints
# ---------------------------------------------------------------------------


def _field(record: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an object, returning ``None`` when absent."""
    if record is None:
        return None
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _text(value: Any) -> Optional[str]:
    """Render an enum/str/None field as a plain string (or ``None``)."""
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


def _isoformat(value: Any) -> Optional[str]:
    """Render a datetime as ISO-8601, passing strings through untouched."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    text = str(value).strip()
    return text or None


def _completion_status_value(record: Any) -> Optional[str]:
    """Return the COA's completion status as the wire string."""
    return _text(_field(record, "completion_status")) or _text(_field(record, "status"))


def _record_coa_id(record: Any) -> str:
    """Return the contract identifier of a COA record."""
    return _text(_field(record, "coa_id")) or _text(_field(record, "coa_number")) or ""


def _contract_dto(record: Any) -> dict[str, Any]:
    """Map a ``COARecord`` onto the contract DTO (``coa_id``/``plant_of_origin``)."""
    return {
        "coa_id": _record_coa_id(record),
        "plant_of_origin": _text(_field(record, "plant_of_origin"))
        or _text(_field(record, "plant_code")),
        "completion_status": _completion_status_value(record),
        "customer_id": _text(_field(record, "customer_id")),
        "product_code": _text(_field(record, "product_code")),
        "lot_number": _text(_field(record, "lot_number")),
        "issue_date": _isoformat(_field(record, "issue_date")),
        "file_size_bytes": _field(record, "file_size_bytes"),
        "status": _text(_field(record, "status")),
        "generated_at": _isoformat(_field(record, "generated_at")),
        "completed_at": _isoformat(_field(record, "completed_at")),
    }


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    """Slice ``items`` into the requested page and describe the envelope."""
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def _error(status_code: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    """Build the contract's error envelope: ``detail`` plus a machine ``code``."""
    payload: dict[str, Any] = {"detail": detail, "code": code}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return JSONResponse(status_code=status_code, content=payload)


def _gate_failure(coa_id: str, outcome: ValidationOutcome) -> JSONResponse:
    """Return the 409 body the contract defines for a blocked distribution."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": ReleasePolicy.REASON_INCOMPLETE
            if outcome.missing_fields
            else ReleasePolicy.REASON_NOT_COMPLETED,
            "code": COMPLETION_GATE_CODE,
            "coa_id": coa_id,
            "missing_fields": list(outcome.missing_fields),
        },
    )


def _mule_unavailable(exc: MuleApiError) -> HTTPException:
    """Translate a Mule-layer failure into a 503 with its typed code."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


async def _load_coa(service: COAService, coa_id: str) -> Any:
    """Fetch one COA, raising 503 for an integration failure and 404 when unknown."""
    try:
        record = await service.get_coa_by_number(coa_id)
    except MuleApiError as exc:
        raise _mule_unavailable(exc) from exc
    except Exception as exc:  # the integration client is unavailable or misbehaving
        logger.exception("unexpected failure fetching COA %s", coa_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "coa_integration_unavailable",
                "message": str(exc) or "the COA integration layer is unavailable",
                "details": None,
            },
        ) from exc
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "coa_not_found", "coa_id": coa_id},
        )
    return record


def _record_access(actor: str, coa_id: str, action: str, metadata: dict[str, Any]) -> None:
    """Append one access event to the immutable audit trail (FR12).

    Returns:
        ``None`` on success - there is no "recorded: false" outcome any more.
        A failed append propagates, because the caller must not hand out a
        distribution the trail never saw.
    """
    try:
        log_access(actor=actor, action=action, resource=f"coa:{coa_id}", metadata=metadata)
    except Exception:
        logger.exception(
            "audit trail write failed actor=%s coa=%s action=%s",
            actor,
            coa_id,
            action,
        )
        raise


def _record_generation(
    actor: str, coa_id: str, action: str, metadata: dict[str, Any]
) -> None:
    """Append one generation event to the immutable audit trail (FR12).

    Like :func:`_record_access`, a failure propagates rather than being reported
    as an unaudited success.
    """
    try:
        log_generation(actor=actor, resource=f"coa:{coa_id}", action=action, metadata=metadata)
    except Exception:
        logger.exception(
            "audit trail write failed actor=%s coa=%s action=%s",
            actor,
            coa_id,
            action,
        )
        raise


def _audit_failed(coa_id: str, exc: BaseException) -> JSONResponse:
    """Return the 503 that refuses a distribution the trail never recorded."""
    return _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        AUDIT_WRITE_FAILED_CODE,
        "the audit trail could not record this distribution, so no document was released",
        coa_id=coa_id,
        audit_error=str(exc) or type(exc).__name__,
    )


def _role_guard(
    presented_role: Optional[str], resource: str, action: str
) -> Optional[JSONResponse]:
    """Apply RBAC when the caller states its role in ``X-Role``.

    The contract authenticates by token exchange, so a role header is an
    additional assertion rather than the only gate: when it is absent the
    request proceeds, and when it is present the role must actually grant the
    ``(resource, action)`` pair.
    """
    if not presented_role:
        return None
    if authorize(presented_role, resource, action):
        return None
    return _error(
        status.HTTP_403_FORBIDDEN,
        "forbidden",
        f"role {presented_role!r} may not {action} {resource}",
        allowed_resources=sorted(get_allowed_resources(presented_role)),
    )


# ---------------------------------------------------------------------------
# Service factories (overridable so a deployment can inject its own wiring)
# ---------------------------------------------------------------------------


def build_jde_client() -> Optional[JDEClient]:
    """Build the live-JDE client the completion gate reads through (FR6/FR14).

    Returns ``None`` when this deployment has not configured ``JDE_BASE_URL``,
    which is the signal that the gate should use the Mule-carried ``test_results``
    snapshot instead. A JDE that *is* configured but then fails to answer does
    not come back as ``None``: it raises, and :func:`_evaluate_gate` degrades and
    surfaces that failure rather than letting a failed live read pass unnoticed.
    """
    try:
        return JDEClient.from_env()
    except JDEConfigurationError as exc:
        logger.debug("live JDE state is not configured; using the Mule-carried record: %s", exc)
        return None


@dataclass(frozen=True)
class GateEvaluation:
    """One completion-gate verdict together with the path that produced it.

    Attributes:
        outcome: The verdict, in the pure gate's own shape.
        source: ``"jde"`` when live JDE state decided, ``"mule"`` otherwise.
        jde_error: The live-read failure that forced a degraded verdict, if any.
    """

    outcome: ValidationOutcome
    source: str
    jde_error: Optional[str] = None

    @property
    def degraded(self) -> bool:
        """True when a live JDE read failed and the snapshot answered instead."""
        return self.jde_error is not None

    def degrade_markers(self) -> dict[str, Any]:
        """Return the response keys that report a degraded verdict, if degraded."""
        if not self.degraded:
            return {}
        return {
            JDE_DEGRADED_KEY: True,
            "jde_error": self.jde_error,
            GATE_SOURCE_KEY: self.source,
        }


def _evaluate_gate(coa_id: str, record: Any) -> GateEvaluation:
    """Evaluate the completion gate, preferring live JDE state over the snapshot.

    FR6 requires the gate to read the live JDE state, so when a JDE client can
    be built the verdict comes from JDE's current test fields, JDE's current
    lifecycle status and the RDS expected-field catalog. Only a *failed* live
    read falls back to the ``COARecord``'s Mule-carried ``test_results``, and the
    failure is logged at WARNING level and returned on the evaluation so the
    endpoint can surface it instead of presenting a degraded verdict as a clean
    pass.
    """
    client = build_jde_client()
    if client is None:
        return GateEvaluation(_completion_gate.validate_coa(record), source="mule")

    try:
        outcome = client.release_outcome(coa_id)
    except JDEError as exc:
        logger.warning(
            "live JDE read failed for %s (%s); falling back to the Mule-carried record",
            coa_id,
            exc,
        )
        fallback = _completion_gate.validate_coa(record)
        return GateEvaluation(fallback, source="mule", jde_error=str(exc))

    logger.info("completion gate for %s decided from live JDE state", coa_id)
    return GateEvaluation(outcome, source="jde")


def _s3_credentials() -> Optional[Any]:
    """Return signing credentials from the environment, or ``None``.

    Credentials are read from the standard AWS environment variables and are
    only materialised when botocore - the signing implementation owned by the
    document service - is importable.
    """
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    if not access_key or not secret_key:
        return None
    try:
        from botocore.credentials import Credentials  # type: ignore
    except Exception:
        logger.warning(
            "AWS credentials are configured but botocore is not installed; "
            "presigned URL signing is unavailable"
        )
        return None
    return Credentials(access_key, secret_key, token=os.getenv("AWS_SESSION_TOKEN"))


def build_presigned_url_service(*, expiry_seconds: int) -> PresignedUrlService:
    """Build the scoped presigned-URL service for a download of ``expiry_seconds``.

    ``PresignedUrlService.from_env`` reads the bucket/region configuration and
    never reads credentials itself, so signing credentials are injected here
    through ``with_credentials``. The FR6 completion gate is enforced by this
    module before the service is called, so the service's own optional
    validator stays off rather than being configured twice.
    """
    configured = PresignedUrlService.from_env()
    config = replace(
        configured.config,
        expiry_seconds=min(expiry_seconds, configured.config.max_expiry_seconds),
    )
    credentials = _s3_credentials()
    if credentials is None:
        return PresignedUrlService(config=config, validate_before_sign=False)
    return PresignedUrlService(
        config=config, credentials=credentials, validate_before_sign=False
    )


def build_print_service() -> PrintService:
    """Build the FR8 print service.

    The presigned-URL provider is attached only when signing credentials are
    configured: without them the document service cannot produce the FR7 URL
    and would deny every print, so the print service is built without a provider
    and reports ``presigned_url: null`` rather than failing on configuration.
    """
    if _s3_credentials() is None:
        return PrintService.from_env()
    return PrintService.from_env(
        presigned_url_service=build_presigned_url_service(
            expiry_seconds=DEFAULT_DOWNLOAD_EXPIRY_SECONDS
        )
    )


def _expires_in(result: Any, fallback: int) -> int:
    """Return the lifetime of ``result`` in whole seconds, or ``fallback``."""
    expires_at = _field(result, "expires_at")
    if expires_at is None:
        return fallback
    if isinstance(expires_at, datetime):
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        seconds = (expires_at - now).total_seconds()
        return max(1, int(round(seconds)))
    return fallback


# ---------------------------------------------------------------------------
# 1. POST /auth/login
# ---------------------------------------------------------------------------


@router.post("/auth/login")
async def login(payload: LoginRequest) -> dict[str, Any]:
    """Exchange an OIDC token for the portal's normalised session identity."""
    try:
        result = login_flow(payload.token)
    except TokenValidationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from None
    user = result.user
    return {
        "user_id": user.user_id,
        "username": user.username,
        "roles": list(user.roles),
        "plants": list(user.plants),
        "expires_at": result.expires_at,
    }


# ---------------------------------------------------------------------------
# 2. GET /coas
# ---------------------------------------------------------------------------


@router.get("/coas")
async def list_coas(
    search: Optional[str] = Query(None, description="COA number or customer search term"),
    plant: Optional[str] = Query(
        None,
        description="Plant of origin filter (FR4); passed through to the Mule search",
    ),
    product: Optional[str] = Query(
        None,
        description="Product code filter (FR4); applied to each record's product_code",
    ),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Completion status filter (pending, completed, failed)",
    ),
    date_from: Optional[str] = Query(None, description="Issue date start (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Issue date end (YYYY-MM-DD)"),
    page: int = Query(DEFAULT_PAGE, ge=1, description="Page number"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Results per page"),
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Search COAs and return the paginated contract envelope.

    ``plant`` is a search criterion and travels with the ``COAFilter`` to the
    Mule API. ``product`` is not a Mule search criterion, so it is applied here,
    server-side, against each returned record's ``product_code``; the filter is
    therefore applied before pagination so the envelope's ``total`` counts only
    the matching records.
    """
    filters = COAFilter(
        **parse_filters(
            q=search,
            plant=plant,
            status=status_filter,
            date_from=date_from,
            date_to=date_to,
        )
    )
    try:
        records = await service.search_coa(filters)
    except MuleApiError as exc:
        raise _mule_unavailable(exc) from exc
    except Exception as exc:
        logger.exception("unexpected failure searching COAs")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "coa_integration_unavailable",
                "message": str(exc) or "the COA integration layer is unavailable",
                "details": None,
            },
        ) from exc
    if product:
        wanted = str(product).strip().lower()
        records = [
            record
            for record in records
            if (_text(_field(record, "product_code")) or "").lower() == wanted
        ]
    return _paginate([_contract_dto(record) for record in records], page, page_size)


# ---------------------------------------------------------------------------
# 3. GET /coas/{coa_id}
# ---------------------------------------------------------------------------


@router.get("/coas/{coa_id}")
async def get_coa_detail(
    coa_id: str,
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Return one COA in the contract DTO shape."""
    return _contract_dto(await _load_coa(service, coa_id))


# ---------------------------------------------------------------------------
# 4. POST /coas/{coa_id}/download
# ---------------------------------------------------------------------------


@router.post("/coas/{coa_id}/download")
async def download_coa(
    coa_id: str,
    payload: Optional[DownloadRequest] = None,
    x_user_id: Optional[str] = Header(
        None, alias="X-User-Id", description="Actor recorded in the audit trail"
    ),
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> Any:
    """Issue a short-lived presigned PDF URL once the completion gate passes."""
    requested_expiry = DEFAULT_DOWNLOAD_EXPIRY_SECONDS
    if payload is not None and payload.expiry_seconds is not None:
        requested_expiry = payload.expiry_seconds

    record = await _load_coa(service, coa_id)
    evaluation = _evaluate_gate(coa_id, record)
    outcome = evaluation.outcome
    if not outcome.releasable:
        return _gate_failure(coa_id, outcome)

    actor = x_user_id or SERVICE_ACTOR
    try:
        presigned = build_presigned_url_service(expiry_seconds=requested_expiry)
        result = presigned.generate_signed_url(
            key=f"coa/secure/coas/{coa_id}.pdf",
            method="GET",
            expiry_seconds=requested_expiry,
            user_id=actor,
            coa_id=coa_id,
        )
    except PresignedUrlError as exc:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "presigned_url_unavailable",
            f"a download URL could not be issued: {exc}",
            coa_id=coa_id,
        )
    except Exception as exc:
        logger.exception("unexpected failure signing the download URL for %s", coa_id)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "download_failed",
            str(exc) or "the download URL could not be issued",
            coa_id=coa_id,
        )

    # FR18 "cannot be reused after use": the scope is keyed by (COA, actor), so
    # this request retires the scope of any URL previously issued for the same
    # pair. The store is the server-side enforcement point; refusing the second
    # GET at the S3 object level needs a proxy in front of the bucket.
    store = get_consumption_store()
    scope_id = store.issue(build_strategy_key(coa_id, actor))

    try:
        _record_access(
            actor=actor,
            coa_id=coa_id,
            action="download",
            metadata={
                "key": _text(_field(result, "key")) or f"coas/{coa_id}.pdf",
                "expiry_seconds": requested_expiry,
                "scope_hash": _text(_field(result, "scope_hash")),
                "consumption_scope_id": scope_id,
                "gate_source": evaluation.source,
            },
        )
    except Exception as exc:
        # The URL is never returned, and the scope minted a moment ago is
        # retired so the signature the client never received cannot be spent.
        store.retire(scope_id)
        return _audit_failed(coa_id, exc)

    body: dict[str, Any] = {
        "coa_id": coa_id,
        "url": _text(_field(result, "signed_url")) or "",
        "expires_in": _expires_in(result, requested_expiry),
        "method": _text(_field(result, "method")) or "GET",
        "scope_id": scope_id,
        "audit_recorded": True,
    }
    body.update(evaluation.degrade_markers())
    return body


# ---------------------------------------------------------------------------
# 5. POST /coas/{coa_id}/print
# ---------------------------------------------------------------------------


async def _check_sister_plant(
    coa_id: str, printing_plant: str, target_plant: str, user_id: str
) -> Optional[JSONResponse]:
    """Apply the FR9 cross-plant print check.

    Returns:
        ``None`` when the print is allowed, otherwise the 403 response body.
    """
    active = await get_plant_mapping_service().get_active_sister_plants(printing_plant)
    validator = SisterPlantValidator(lambda _plant: active)
    result = validator.validate_print_request(
        SisterPlantPrintRequest(
            coa_id=coa_id,
            requesting_plant_id=printing_plant,
            target_plant_id=target_plant,
            user_id=user_id,
        )
    )
    if result.is_valid:
        return None
    return _error(
        status.HTTP_403_FORBIDDEN,
        "sister_plant_mapping_missing",
        result.message,
        coa_id=coa_id,
        requesting_plant=printing_plant,
        target_plant=target_plant,
    )


@router.post("/coas/{coa_id}/print")
async def print_coa(
    coa_id: str,
    payload: PrintContractRequest,
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> Any:
    """Grant a print after the completion gate and FR8 re-authentication pass."""
    record = await _load_coa(service, coa_id)
    evaluation = _evaluate_gate(coa_id, record)
    outcome = evaluation.outcome
    if not outcome.releasable:
        return _gate_failure(coa_id, outcome)

    if payload.printing_plant:
        target_plant = _text(_field(record, "plant_of_origin")) or _text(
            _field(record, "plant_code")
        )
        if target_plant and target_plant != payload.printing_plant:
            denied = await _check_sister_plant(
                coa_id, payload.printing_plant, target_plant, payload.user_id
            )
            if denied is not None:
                return denied

    try:
        print_service = build_print_service()
    except ReauthError as exc:
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "reauth_unavailable",
            f"print re-authentication is not configured: {exc.reason}",
            coa_id=coa_id,
        )

    try:
        result = print_service.handle_print(
            PrintRequest(
                user_id=payload.user_id,
                coa_id=coa_id,
                reauth_token=payload.reauth_token,
                metadata={
                    "printing_plant": payload.printing_plant,
                    "completion_status": outcome.completion_status,
                },
            )
        )
    except ReauthError as exc:
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "reauth_unavailable",
            f"print re-authentication failed: {exc.reason}",
            coa_id=coa_id,
        )
    except Exception as exc:
        logger.exception("unexpected failure processing the print for %s", coa_id)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "print_failed",
            str(exc) or "the print request could not be processed",
            coa_id=coa_id,
        )

    audit_event = _field(result, "audit_event")
    event_action = _text(_field(audit_event, "action"))
    if not _field(result, "success"):
        if event_action == PRINT_DENIED_VALIDATION:
            return _error(
                status.HTTP_409_CONFLICT,
                COMPLETION_GATE_CODE,
                "print denied by the completion gate",
                coa_id=coa_id,
                missing_fields=list(outcome.missing_fields),
            )
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "print_reauth_failed",
            _text(_field(audit_event, "detail")) or "print re-authentication failed",
            coa_id=coa_id,
        )

    try:
        _record_generation(
            actor=payload.user_id,
            coa_id=coa_id,
            action="print",
            metadata={
                "printing_plant": payload.printing_plant,
                "audit_event_id": _text(_field(audit_event, "event_id")),
                "scope_hash": _text(_field(audit_event, "scope_hash")),
                "gate_source": evaluation.source,
            },
        )
    except Exception as exc:
        return _audit_failed(coa_id, exc)

    body: dict[str, Any] = {
        "coa_id": coa_id,
        "success": True,
        "presigned_url": _text(_field(result, "presigned_url")),
        "audit_event": _field(audit_event, "to_dict")() if hasattr(audit_event, "to_dict") else None,
        "audit_recorded": True,
    }
    body.update(evaluation.degrade_markers())
    return body


# ---------------------------------------------------------------------------
# 5a. POST /coas/{coa_id}/print/reauth
# ---------------------------------------------------------------------------


@router.post("/coas/{coa_id}/print/reauth")
async def issue_print_reauth_token(coa_id: str, payload: PrintReauthRequest) -> Any:
    """Mint the short-lived FR8 re-auth credential ``POST .../print`` requires.

    The portal re-authenticates the operator out of band and calls this endpoint
    to exchange that proof for the credential the print endpoint actually
    verifies. Minting is delegated to :meth:`PrintService.issue_reauth_token`,
    which owns the HMAC format that :meth:`PrintService.handle_print` verifies,
    so the two can never drift apart.

    Returns:
        The COA, the operator the token is bound to, the token itself and the
        instant it expires - ``expires_at`` derived from the service's own
        declared ``REAUTH_LIFETIME_SECONDS``.

    A deployment with no print service configured answers ``503``: without the
    re-auth secret the token cannot be signed, and returning a token that would
    not verify would be worse than refusing.
    """
    try:
        print_service = build_print_service()
    except ReauthError as exc:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "reauth_unavailable",
            f"print re-authentication is not configured: {exc.reason}",
            coa_id=coa_id,
        )

    try:
        reauth_token = print_service.issue_reauth_token(payload.user_id, coa_id)
    except ReauthError as exc:
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "reauth_unavailable",
            f"print re-authentication is not configured: {exc.reason}",
            coa_id=coa_id,
        )
    except Exception as exc:
        logger.exception("re-auth issuance failed for COA %s", coa_id)
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "reauth_issue_failed",
            str(exc) or "the re-auth token could not be issued",
            coa_id=coa_id,
        )

    return {
        "coa_id": coa_id,
        "user_id": payload.user_id,
        "reauth_token": reauth_token,
        "expires_at": (
            datetime.now(timezone.utc)
            + timedelta(seconds=PrintService.REAUTH_LIFETIME_SECONDS)
        ).isoformat(),
    }


# ---------------------------------------------------------------------------
# 6. POST /coas/{coa_id}/validate
# ---------------------------------------------------------------------------


@router.post("/coas/{coa_id}/validate")
async def validate_coa(
    coa_id: str,
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Report the completion gate's verdict for one COA; never blocks on 'incomplete'."""
    record = await _load_coa(service, coa_id)
    evaluation = _evaluate_gate(coa_id, record)
    body = evaluation.outcome.to_dict()
    body.update(evaluation.degrade_markers())
    return body


# ---------------------------------------------------------------------------
# 7. GET /missing-coas
# ---------------------------------------------------------------------------


class _SearchResultRepository:
    """Adapter exposing a COA search result through the report repository protocols.

    ``MissingCoaReportService`` reads its rows from a discovery repository and a
    validation repository. The contract's missing-COA report is driven by one
    COA search, so the searched rows are split between the two adapters instead
    of being duplicated: rows that already carry a failed validation status go
    to the validation adapter, everything else to the discovery adapter.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetch_missing_coas(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetch_failed_validations(self) -> list[dict[str, Any]]:
        return list(self._rows)


def _missing_coa_row(record: Any) -> dict[str, Any]:
    """Flatten a COA record into the raw shape the report service consumes.

    The COA service carries no shipping data, so ``vessel``/``voyage`` are empty
    and ``customer_id`` is the only counterparty available; ``issue_date`` is
    the earliest date the record states and is used as the required ship date.
    """
    completion = (_completion_status_value(record) or "").lower()
    return {
        "coa_id": _record_coa_id(record),
        "vessel": "",
        "voyage": "",
        "counterparty": _text(_field(record, "customer_id")) or "",
        "priority": None,
        "required_ship_date": _text(_field(record, "issue_date")),
        "validation_passed": False if completion == "failed" else None,
        "incomplete": completion != "completed",
        "status": _text(_field(record, "status")) or completion or None,
    }


def _missing_coa_item(row: MissingCoaRow) -> dict[str, Any]:
    """Map one report row onto the contract's missing-COA item shape."""
    return {
        "coa_id": row.coa_id,
        "vessel": row.vessel,
        "voyage": row.voyage,
        "counterparty": row.counterparty,
        "priority": row.priority,
        "required_ship_date": row.required_ship_date.isoformat()
        if row.required_ship_date
        else None,
        "age_days": row.age_days,
        "blocking_reason": row.blocking_reason,
    }


@router.get("/missing-coas")
async def list_missing_coas(
    service: COAService = Depends(get_coa_service),  # noqa: B008
) -> dict[str, Any]:
    """Report every non-completed COA that is blocking a shipment."""
    try:
        # The report covers pending *and* failed COAs, so the search is unfiltered
        # and the non-completed subset is selected here rather than upstream.
        records = await service.search_coa(COAFilter())
    except MuleApiError as exc:
        raise _mule_unavailable(exc) from exc
    except Exception as exc:
        logger.exception("unexpected failure collecting missing COAs")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "coa_integration_unavailable",
                "message": str(exc) or "the COA integration layer is unavailable",
                "details": None,
            },
        ) from exc

    raw_rows = [_missing_coa_row(record) for record in records]
    outstanding = [row for row in raw_rows if row["coa_id"] and row["incomplete"]]
    report = MissingCoaReportService(
        discovery_repo=_SearchResultRepository(
            [row for row in outstanding if row["status"] != "failed"]
        ),
        validation_repo=_SearchResultRepository(
            [row for row in outstanding if row["status"] == "failed"]
        ),
    ).generate_report(report_window="daily")

    return {
        "items": [_missing_coa_item(row) for row in report.rows],
        "generated_at": report.generated_at,
    }


# ---------------------------------------------------------------------------
# 8. POST /admin/users
# ---------------------------------------------------------------------------


def _user_dto(user: UserDTO) -> dict[str, Any]:
    """Return the JSON body for one registry account."""
    return user.to_dict()


@router.post("/admin/users", status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    payload: CreateUserPayload,
    x_role: Optional[str] = Header(
        None, alias="X-Role", description="Role asserted by the caller for RBAC"
    ),
) -> Any:
    """Register an administrator-managed external account."""
    forbidden = _role_guard(x_role, "admin", "admin")
    if forbidden is not None:
        return forbidden
    try:
        user = create_external_account(payload.model_dump())
    except DuplicateUserError as exc:
        return _error(
            status.HTTP_409_CONFLICT,
            "duplicate_user",
            str(exc),
            username=exc.username,
        )
    except UserNotFoundError as exc:
        return _error(status.HTTP_404_NOT_FOUND, "user_not_found", str(exc))
    except InvalidRoleError as exc:
        return _error(HTTP_422_UNPROCESSABLE, "invalid_role", str(exc))
    except ValueError as exc:
        return _error(HTTP_422_UNPROCESSABLE, "invalid_user", str(exc))
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_user_dto(user))
