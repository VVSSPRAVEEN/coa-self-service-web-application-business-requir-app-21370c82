"""Query-filter parsing for COA search and listing (FR4 / FR5).

``parse_filters`` is the single translation point between the raw query
parameters declared on ``GET /coa/search`` in :mod:`src.api.mule.endpoints` and
the filter mapping handed to ``COAService.search_coa``.  It returns a plain
``dict`` whose keys are always exactly ``q``, ``plant``, ``status``,
``start_date`` and ``end_date``; empty or unparsable values are omitted so the
mapping can be passed straight through to the Mule API client.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "FILTER_FIELDS",
    "STATUS_ALIASES",
    "VALID_STATUSES",
    "COAFilterValidationError",
    "apply_filters",
    "matches_filters",
    "normalize_date",
    "normalize_status",
    "parse_filters",
]

FILTER_FIELDS: Sequence[str] = ("q", "plant", "status", "start_date", "end_date")

VALID_STATUSES: Sequence[str] = ("pending", "completed", "failed")

STATUS_ALIASES: Mapping[str, str] = {
    "complete": "completed",
    "completed": "completed",
    "done": "completed",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "incomplete": "pending",
    "pending": "pending",
    "open": "pending",
    "in_progress": "pending",
    "in-progress": "pending",
}


class COAFilterValidationError(ValueError):
    """Raised when a filter value cannot be coerced into a supported type."""


def _clean(value: Any) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` when it is empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_status(value: Any) -> str | None:
    """Return the canonical completion status for ``value`` (or ``None``).

    Aliases such as ``complete``, ``incomplete`` or ``error`` are folded onto
    the three canonical values (``pending``, ``completed``, ``failed``).  An
    unrecognised status is kept in normalised lower-case form instead of being
    dropped, so a status introduced upstream still filters rather than being
    silently ignored.
    """
    text = _clean(value)
    if text is None:
        return None
    key = text.lower().replace(" ", "_")
    return STATUS_ALIASES.get(key, key)


def normalize_date(value: Any, *, field_name: str = "date") -> str | None:
    """Coerce ``value`` into an ISO ``YYYY-MM-DD`` string, or ``None``.

    ``date``/``datetime`` objects and ISO-like strings are accepted.  A value
    that cannot be interpreted as a date is dropped and logged rather than
    raised, so a malformed query parameter can never turn a search request into
    an unhandled 500 response.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = _clean(value)
    if text is None:
        return None

    candidate = text.replace("/", "-")
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        logger.warning("Ignoring unparsable %s filter value: %r", field_name, value)
        return None


def parse_filters(
    *,
    q: str | None = None,
    plant: str | None = None,
    status: str | None = None,
    start_date: Any | None = None,
    end_date: Any | None = None,
    search: str | None = None,
    plant_code: str | None = None,
    completion_status: str | None = None,
    date_from: Any | None = None,
    date_to: Any | None = None,
) -> dict[str, Any]:
    """Build the canonical filter mapping consumed by the COA service layer.

    Only the keys defined by :data:`FILTER_FIELDS` can appear in the result,
    and a key is present only when it carries a usable value.  ``search``,
    ``plant_code``, ``completion_status``, ``date_from`` and ``date_to`` are
    accepted as friendly aliases of the canonical names so alternative callers
    (Mule scheduler payloads, tests) do not have to re-map the fields.
    """
    normalized_q = _clean(q) or _clean(search)
    normalized_plant = _clean(plant) or _clean(plant_code)
    normalized_status = normalize_status(status if status is not None else completion_status)
    normalized_start = normalize_date(
        start_date if start_date is not None else date_from,
        field_name="start_date",
    )
    normalized_end = normalize_date(
        end_date if end_date is not None else date_to,
        field_name="end_date",
    )

    if normalized_start and normalized_end and normalized_start > normalized_end:
        logger.warning(
            "start_date %s is after end_date %s; both are passed through unchanged",
            normalized_start,
            normalized_end,
        )

    filters: dict[str, Any] = {}
    if normalized_q:
        filters["q"] = normalized_q
    if normalized_plant:
        filters["plant"] = normalized_plant
    if normalized_status:
        filters["status"] = normalized_status
    if normalized_start:
        filters["start_date"] = normalized_start
    if normalized_end:
        filters["end_date"] = normalized_end
    return filters


def _read(source: Any, field: str) -> Any:
    """Read ``field`` from a mapping or an object, returning ``None`` if absent."""
    if isinstance(source, Mapping):
        return source.get(field)
    return getattr(source, field, None)


def matches_filters(coa: Any, filters: Mapping[str, Any]) -> bool:
    """Return ``True`` when ``coa`` satisfies every criterion in ``filters``."""
    if not filters:
        return True

    term = filters.get("q")
    if term:
        haystack = " ".join(
            str(_read(coa, field) or "")
            for field in ("coa_number", "coa_no", "customer_name", "customer")
        ).lower()
        if str(term).lower() not in haystack:
            return False

    plant = filters.get("plant")
    if plant:
        coa_plant = _read(coa, "plant_code") or _read(coa, "plant") or ""
        if str(coa_plant).lower() != str(plant).lower():
            return False

    status = filters.get("status")
    if status and normalize_status(_read(coa, "status")) != status:
        return False

    start = filters.get("start_date")
    end = filters.get("end_date")
    if start or end:
        issued = normalize_date(_read(coa, "issue_date"), field_name="issue_date")
        if issued is not None:
            if start and issued < str(start):
                return False
            if end and issued > str(end):
                return False

    return True


def apply_filters(items: Iterable[Any], filters: Mapping[str, Any]) -> list[Any]:
    """Return the subset of ``items`` that satisfy ``filters``."""
    if not filters:
        return list(items)
    return [item for item in items if matches_filters(item, filters)]
