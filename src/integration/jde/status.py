"""Read a COA's live lifecycle status out of JDE (FR6, FR14).

The release decision needs two independent facts from JDE: *which* test fields
exist and are satisfied (:mod:`~src.integration.jde.reader`) and *whether the COA
itself has been released upstream* (this module). They are fetched separately
because JDE keeps them in separate documents, and because a COA whose tests all
passed but whose header is still open must still be blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .client import CoaStatusPath, JDEPayloadError, JDETransport

__all__ = [
    "COA_COMPLETED_STATUSES",
    "JDECOAStatus",
    "jde_status_is_final",
    "read_coa_id",
    "read_completed_at",
    "read_completion_status",
]

#: ``completion_status`` values JDE publishes for a COA that may be released.
#: The set is deliberately wider than the Mule vocabulary because JDE reports
#: the document lifecycle ("issued"/"released") alongside the test lifecycle.
COA_COMPLETED_STATUSES = frozenset({"completed", "complete", "issued", "released", "closed"})

#: Keys under which JDE may publish the lifecycle status.
_STATUS_KEYS = ("completion_status", "status", "lifecycle_status", "coa_status")

#: Keys under which JDE may echo the COA identifier.
_ID_KEYS = ("coa_id", "coa_number", "coa", "id")

#: Boolean keys JDE may publish alongside the textual status.
_DONE_KEYS = ("completed", "is_complete", "released", "is_released")


def _clean(value: Any) -> str | None:
    """Return ``value`` as a trimmed lowercase string, or ``None`` when empty."""
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text or None


def read_completion_status(payload: Any) -> str | None:
    """Return the lifecycle status JDE published, or ``None`` when it published none.

    Args:
        payload: The decoded JDE status document.

    Returns:
        The lower-cased status string, or ``None``.

    Raises:
        JDEPayloadError: If ``payload`` is neither a mapping nor ``None``.
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise JDEPayloadError(
            f"expected a JDE object of COA status, got {type(payload).__name__}"
        )

    for key in _STATUS_KEYS:
        status = _clean(payload.get(key))
        if status:
            return status

    for key in _DONE_KEYS:
        flag = payload.get(key)
        if isinstance(flag, bool):
            return "completed" if flag else "pending"
    return None


def read_coa_id(payload: Any) -> str:
    """Return the COA identifier JDE echoed, or an empty string when it echoed none."""
    if not isinstance(payload, Mapping):
        return ""
    for key in _ID_KEYS:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def read_completed_at(payload: Any) -> str | None:
    """Return the ISO-8601 completion timestamp JDE published, if any."""
    if not isinstance(payload, Mapping):
        return None
    for key in ("completed_at", "completion_timestamp", "closed_at", "issued_at"):
        value = payload.get(key)
        if not value:
            continue
        stamp = getattr(value, "isoformat", None)
        return str(stamp() if callable(stamp) else value).strip() or None
    return None


def jde_status_is_final(payload: Any) -> bool:
    """Decide whether a JDE status document proves the COA has been released.

    Unlike the pure gate - which treats an *absent* status as final because the
    Mule record's own status is the authority - a live JDE read is judged
    fail-closed: a document that states no final status is not proof of release,
    so the COA stays blocked and the 409 reads "blocked until the upstream
    completion status is 'completed'".

    Raises:
        JDEPayloadError: If ``payload`` is not a JDE status object.
    """
    return read_completion_status(payload) in COA_COMPLETED_STATUSES


class JDECOAStatus:
    """Read live COA lifecycle status out of JDE (FR6, FR14).

    Args:
        transport: The JDE transport to read through; injected so the status
            reader can be exercised without a live JDE instance.
    """

    def __init__(self, transport: JDETransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> JDETransport:
        """The transport this reader reads through."""
        return self._transport

    def fetch_coa_status(self, coa_id: str) -> dict[str, Any]:
        """Return the live lifecycle status JDE holds for ``coa_id``.

        Args:
            coa_id: The COA number JDE keys its status document by.

        Returns:
            A normalised status mapping with ``coa_id``, ``completion_status``,
            ``completed_at`` and ``is_complete``. ``completion_status`` is
            ``None`` when JDE published no status at all, which the gate reads as
            "not proven released".

        Raises:
            ValueError: If ``coa_id`` is empty.
            JDEError: If the read fails or JDE answers with an unusable payload.
        """
        key = str(coa_id or "").strip()
        if not key:
            raise ValueError("a COA id is required")

        payload = self._transport.get_json(CoaStatusPath, {"coa_id": key})
        status = read_completion_status(payload)
        return {
            "coa_id": read_coa_id(payload) or key,
            "completion_status": status,
            "is_complete": status in COA_COMPLETED_STATUSES,
            "completed_at": read_completed_at(payload),
            "raw": dict(payload) if isinstance(payload, Mapping) else {},
        }

    def fetch_status_as_of(self, coa_id: str) -> dict[str, Any]:
        """Return the status document plus the UTC instant it was observed."""
        status = self.fetch_coa_status(coa_id)
        status["observed_at"] = datetime.now(UTC).isoformat()
        return status
