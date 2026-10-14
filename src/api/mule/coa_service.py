from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.audit.trail.models import AuditEventType
from src.audit.trail.repository import AuditRepository
from src.audit.trail.service import AuditTrailService

_logger = logging.getLogger("ventura.coa.api.mule")

# Event type names written into the immutable audit trail (FR12). These mirror
# the event vocabulary owned by src/audit/trail; the trail service is the single
# writer, this module only classifies the business action it wants recorded.
ACCESS_READ = "coa_access_read"
GENERATION_REQUEST = "coa_generation_request"

#: Business action -> the trail's own typed vocabulary. AuditEventType is a
#: closed enum, so the mapping is explicit rather than AuditEventType(value).
_TRAIL_EVENT_TYPES = {
    ACCESS_READ: AuditEventType.ACCESS,
    GENERATION_REQUEST: AuditEventType.GENERATION,
}

_audit_repository: AuditRepository | None = None
_audit_service: AuditTrailService | None = None


def _get_audit_service() -> AuditTrailService:
    """Return the process-wide audit trail service, creating it on first use.

    The trail is append-only and shared by every writer in the process, so the
    service (and its repository) is created exactly once and reused. Creation is
    lazy so importing this module never touches persistence, which keeps the Mule
    client unit-testable with a stub client.
    """
    global _audit_repository, _audit_service
    if _audit_service is None:
        _audit_repository = AuditRepository()
        _audit_service = AuditTrailService(repository=_audit_repository)
    return _audit_service


def reset_audit_service() -> None:
    """Drop the cached audit service so a new store is opened.

    Used by the CLI and by tests that need a deterministic trail contents.
    """
    global _audit_repository, _audit_service
    _audit_repository = None
    _audit_service = None


class CompletionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class COARecord:
    coa_number: str
    customer_id: str | None = None
    plant_code: str | None = None
    lot_number: str | None = None
    product_code: str | None = None
    issue_date: str | None = None
    completion_status: CompletionStatus | None = None
    status: str | None = None
    file_size_bytes: int | None = None
    test_results: Any | None = None
    created_at: datetime | None = None
    generated_at: datetime | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "coa_number": self.coa_number,
            "customer_id": self.customer_id,
            "plant_code": self.plant_code,
            "lot_number": self.lot_number,
            "product_code": self.product_code,
            "issue_date": self.issue_date,
            "completion_status": self.completion_status.value if self.completion_status else None,
            "status": self.status or (self.completion_status.value if self.completion_status else None),
            "file_size_bytes": self.file_size_bytes,
            "test_results": self.test_results,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "completed_at": self.completed_at,
        }


@dataclass
class COASearchRequest:
    customer_id: str | None = None
    plant_code: str | None = None
    coa_number: str | None = None
    lot_number: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    completion_status: CompletionStatus | None = None
    page: int = 1
    page_size: int = 25


@dataclass
class COAFilter:
    q: str | None = None
    plant: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.q is not None:
            result["q"] = self.q
        if self.plant is not None:
            result["plant"] = self.plant
        if self.start_date is not None:
            result["start_date"] = self.start_date
        if self.end_date is not None:
            result["end_date"] = self.end_date
        if self.status is not None:
            result["status"] = self.status
        return result

    @classmethod
    def from_search_request(cls, request: COASearchRequest) -> COAFilter:
        return cls(
            q=request.coa_number or request.customer_id,
            plant=request.plant_code,
            start_date=request.start_date,
            end_date=request.end_date,
            status=request.completion_status.value if request.completion_status else None,
        )


@dataclass
class MuleApiError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")


MULE_API_BASE_URL = os.getenv("MULE_API_BASE_URL", "http://localhost:8080")


def _record_audit_event(
    event_type: str,
    *,
    actor: str,
    action: str,
    resource: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one event to the immutable audit trail (FR12).

    The business vocabulary this module classifies with (``event_type``,
    ``action``) is translated onto the trail's own vocabulary here, because
    :class:`AuditTrailService` owns the entry model: ``actor`` is the trail's
    ``user_sub``, ``resource`` its ``resource_id``, and ``action`` plus the
    metadata are serialised into ``details`` so nothing the caller recorded is
    dropped on the way into the sealed entry.

    The audit trail must never silently swallow a write failure: a request that
    cannot be recorded is escalated to the caller through the exception rather
    than being reported as an unaudited success.
    """
    service = _get_audit_service()
    trail_event_type = _TRAIL_EVENT_TYPES.get(event_type)
    if trail_event_type is None:
        raise MuleApiError(
            code="audit_write_failed",
            message=f"{event_type!r} is not an auditable event type",
            details={"event_type": event_type, "resource": resource},
        )
    details = json.dumps(
        {"action": action, "metadata": metadata or {}},
        sort_keys=True,
        default=str,
    )
    try:
        service.record_event(
            event_type=trail_event_type,
            user_sub=actor,
            resource_id=resource,
            details=details,
        )
    except Exception as exc:
        _logger.exception(
            "audit trail write failed event_type=%s actor=%s resource=%s",
            event_type,
            actor,
            resource,
        )
        raise MuleApiError(
            code="audit_write_failed",
            message="unable to record the audit event",
            details={"event_type": event_type, "resource": resource},
        ) from exc


def log_access(
    actor: str,
    action: str,
    resource: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a COA read/access event into the immutable audit trail (FR12)."""
    _record_audit_event(
        ACCESS_READ,
        actor=actor,
        action=action,
        resource=resource,
        metadata=metadata,
    )


def log_generation(
    actor: str,
    resource: str,
    action: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a COA generation event into the immutable audit trail (FR12)."""
    _record_audit_event(
        GENERATION_REQUEST,
        actor=actor,
        action=action,
        resource=resource,
        metadata=metadata,
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from Mule into an aware UTC datetime."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_status(value: str | None) -> CompletionStatus | None:
    if not value:
        return None
    try:
        return CompletionStatus(value)
    except ValueError:
        return None


class COAService:
    """COA search/list and detail access over the Mule API (FR4, FR5).

    Every read and every generation request is written to the immutable audit
    trail, so the service satisfies FR12 without the caller having to remember
    to log.
    """

    def __init__(self, mule_client: Any) -> None:
        self._client = mule_client
        self._base_url = MULE_API_BASE_URL

    # -- request construction -------------------------------------------------

    def _build_search_path(self, filters: COAFilter) -> str:
        parts = ["coa", "search"]
        params = []
        for key, value in filters.to_dict().items():
            if value is not None:
                params.append(f"{key}={value}")
        query = "&".join(params)
        path = "/".join(parts)
        return f"{path}?{query}" if query else path

    def _build_detail_path(self, coa_number: str) -> str:
        safe = coa_number.replace("/", "%2F")
        return f"coa/{safe}"

    # -- operations ----------------------------------------------------------

    async def search_coa(self, filters: COAFilter) -> list[COARecord]:
        path = self._build_search_path(filters)
        response = self._client.get(path)
        raw = self._client.ensure_ok(response)
        data = raw.json()
        records = [
            COARecord(
                coa_number=rec.get("coa_number", ""),
                customer_id=rec.get("customer_id"),
                plant_code=rec.get("plant_code"),
                lot_number=rec.get("lot_number"),
                product_code=rec.get("product_code"),
                issue_date=rec.get("issue_date"),
                completion_status=_parse_status(rec.get("completion_status")),
                status=rec.get("status"),
                file_size_bytes=rec.get("file_size_bytes"),
                created_at=_parse_timestamp(rec.get("created_at")),
            )
            for rec in data.get("records", [])
        ]
        log_access(
            actor="coa_service",
            action="search",
            resource="coa:search",
            metadata={
                "filters": filters.to_dict(),
                "result_count": len(records),
                "source": "mule_api",
            },
        )
        return records

    async def get_coa_by_number(self, coa_number: str) -> COARecord | None:
        path = self._build_detail_path(coa_number)
        try:
            response = self._client.get(path)
            raw = self._client.ensure_ok(response)
            data = raw.json()
        except Exception:  # noqa: BLE001 - transport/parse failures degrade to no record
            return None

        record = COARecord(
            coa_number=data.get("coa_number", coa_number),
            customer_id=data.get("customer_id"),
            plant_code=data.get("plant_code"),
            lot_number=data.get("lot_number"),
            product_code=data.get("product_code"),
            issue_date=data.get("issue_date"),
            completion_status=_parse_status(data.get("completion_status")),
            status=data.get("status"),
            file_size_bytes=data.get("file_size_bytes"),
            test_results=data.get("test_results"),
            generated_at=_parse_timestamp(data.get("generated_at")),
            completed_at=data.get("completed_at"),
        )

        log_access(
            actor="coa_service",
            action="read",
            resource=f"coa:{coa_number}",
            metadata={"coa_number": coa_number, "source": "mule_api"},
        )
        return record

    async def generate_coa(self, coa_number: str, user_id: str) -> COARecord:
        path = f"coa/{coa_number}/generate"
        response = self._client.post(path, json={"user_id": user_id})
        raw = self._client.ensure_ok(response)
        data = raw.json()

        record = COARecord(
            coa_number=data.get("coa_number", coa_number),
            customer_id=data.get("customer_id"),
            plant_code=data.get("plant_code"),
            lot_number=data.get("lot_number"),
            product_code=data.get("product_code"),
            issue_date=data.get("issue_date"),
            completion_status=_parse_status(data.get("completion_status")),
            status=data.get("status"),
            file_size_bytes=data.get("file_size_bytes"),
            test_results=data.get("test_results"),
            generated_at=_parse_timestamp(data.get("generated_at")),
            completed_at=data.get("completed_at"),
        )

        log_generation(
            actor=user_id,
            resource=f"coa:{coa_number}",
            action="generate",
            metadata={"coa_number": coa_number, "source": "mule_api"},
        )
        return record

    def _normalize_response(self, response: Any) -> dict[str, Any]:
        raw = self._client.ensure_ok(response)
        data = raw.json()
        return data


class MuleCOAService(COAService):
    """Named alias retained for callers that import the Mule-specific class."""



def get_coa_service() -> COAService:
    """Build a COAService from the deployment's Mule configuration.

    When MULE_API_BASE_URL is not configured to a reachable endpoint the
    service is still constructed, and the first call raises a typed
    MuleApiError rather than a bare NotImplementedError, so callers get one
    consistent error contract.
    """

    class _UnconfiguredClient:
        """Client used when no Mule endpoint is configured for this process."""

        def __init__(self, base_url: str) -> None:
            self._base_url = base_url

        def _fail(self, method: str, path: str) -> MuleApiError:
            return MuleApiError(
                code="mule_not_configured",
                message=(
                    "the Mule COA API is not configured for this deployment; "
                    f"cannot {method} {path}"
                ),
                details={"method": method, "path": path},
            )

        def get(self, path: str) -> Any:
            raise self._fail("GET", path)

        def post(self, path: str, **kwargs: Any) -> Any:
            raise self._fail("POST", path)

        def ensure_ok(self, response: Any) -> Any:
            return response

    return COAService(mule_client=_UnconfiguredClient(MULE_API_BASE_URL))
