"""Read the live lab/test field results for a COA out of JDE (FR6, FR14).

The Mule-carried ``test_results`` block on a :class:`~src.api.mule.coa_service.COARecord`
is a *copy* of what JDE holds at the moment the record was written. This reader
asks JDE instead, so the completion gate evaluates live state rather than a
possibly stale snapshot.

The read is deliberately tolerant about the payload and strict about the
contract:

* JDE may nest the results under ``fields``/``values``/``test_results`` or hand
  back a flat ``{name: entry}`` document. Both are accepted.
* An entry may be a mapping carrying ``status``/``value``, a bare status string,
  a bare result value, or ``null``. All four are normalised to the same shape
  that :class:`~src.validation.completion_gate.validator.CompletionGate`
  already understands, so the downstream gate has one entry shape to evaluate.
* A response that is not a JSON object is a :class:`JDEPayloadError` - the
  reader never invents a passing result out of an unreadable document.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .client import JDEPayloadError, JDETransport, TestFieldsPath, coerce_field_names

__all__ = ["JDEFieldReader", "normalize_field_entries", "normalize_field_entry"]

#: Keys under which JDE may nest its ``{name: entry}`` result document.
_FIELD_CONTAINER_KEYS = ("fields", "values", "test_results", "testResults", "results")

#: Status values that mean the test passed.
_PASSING_STATUS = "pass"

#: Status values JDE publishes for a test that has not passed.
_UNPASSING_STATUSES = frozenset({"fail", "failed", "pending", "incomplete", "error", "rejected"})


def normalize_field_entry(entry: Any) -> dict[str, Any]:
    """Normalise one JDE field entry into the gate's entry shape.

    The keys that are present mirror what JDE actually stated, because
    :class:`~src.validation.completion_gate.validator.CompletionGate` decides
    satisfaction by key presence: an entry with a ``status`` key must say
    ``"pass"``, an entry with only a ``value`` key is satisfied by the value, and
    an entry with neither key is unsatisfied. Emitting a ``status`` key holding
    ``None`` would turn "measured but unlabelled" into "failed", so the key is
    omitted instead.

    Args:
        entry: The raw entry as JDE published it.

    Returns:
        A mapping with ``status`` and/or ``value``, possibly empty.
    """
    if entry is None:
        return {}
    if isinstance(entry, Mapping):
        raw_status = entry.get("status")
        status = str(raw_status).strip().lower() if raw_status is not None else ""
        value = entry.get("value")
        if value is None:
            value = entry.get("result")
        normalized: dict[str, Any] = {}
        if status:
            normalized["status"] = status
        if value is not None:
            normalized["value"] = value
        return normalized

    text = str(entry).strip()
    if not text:
        return {}
    lowered = text.lower()
    if lowered == _PASSING_STATUS or lowered in _UNPASSING_STATUSES:
        return {"status": lowered}
    # A bare scalar is a measured result, which is evidence the field was run.
    return {"value": entry}


def normalize_field_entries(payload: Any) -> dict[str, dict[str, Any]]:
    """Return ``{field_name: {"status": ..., "value": ...}}`` for a JDE payload.

    Args:
        payload: The decoded JDE document.

    Returns:
        The normalised field mapping, empty when JDE published no fields.

    Raises:
        JDEPayloadError: If ``payload`` is neither a mapping nor ``None``.
    """
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise JDEPayloadError(
            f"expected a JDE object of test fields, got {type(payload).__name__}"
        )

    container: Mapping[str, Any] = payload
    for key in _FIELD_CONTAINER_KEYS:
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            container = nested
            break

    # A bare list of ``{"name": ..., "status": ...}`` documents is also accepted.
    entries: Any = container
    if isinstance(entries, (list, tuple)):
        collected: dict[str, Any] = {}
        for item in entries:
            if not isinstance(item, Mapping):
                raise JDEPayloadError("JDE returned a list of test fields with a non-object entry")
            name = item.get("name") or item.get("field") or item.get("field_name")
            if not name:
                raise JDEPayloadError("JDE returned a test field entry without a name")
            collected[str(name)] = {
                key: value for key, value in item.items() if key not in {"name", "field", "field_name"}
            }
        entries = collected

    if not isinstance(entries, Mapping):
        raise JDEPayloadError("JDE returned a test field document of an unsupported shape")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, entry in entries.items():
        name = str(raw_name).strip()
        if not name or name in {"coa_id", "coa_number", "field_id"}:
            continue
        normalized[name] = normalize_field_entry(entry)
    return normalized


class JDEFieldReader:
    """Read live lab/test field results for a COA out of JDE (FR6, FR14).

    Args:
        transport: The JDE transport to read through; injected so the reader can
            be exercised without a live JDE instance.
    """

    def __init__(self, transport: JDETransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> JDETransport:
        """The transport this reader reads through."""
        return self._transport

    def read_test_fields(self, field_id: str) -> dict[str, dict[str, Any]]:
        """Return the live test fields JDE holds for ``field_id``.

        Args:
            field_id: The JDE field/catalog identifier, normally the COA number.

        Returns:
            ``{field_name: entry}`` for every field JDE published, where each
            entry is normalised to the shape the pure completion gate consumes.

        Raises:
            ValueError: If ``field_id`` is empty.
            JDEError: If the read fails or JDE answers with an unusable payload.
        """
        key = str(field_id or "").strip()
        if not key:
            raise ValueError("a JDE field id is required")

        payload = self._transport.get_json(TestFieldsPath, {"field_id": key})
        return normalize_field_entries(payload)

    def read_field_names(self, field_id: str) -> list[str]:
        """Return only the field names JDE currently holds for ``field_id``."""
        return coerce_field_names(self.read_test_fields(field_id).keys())
