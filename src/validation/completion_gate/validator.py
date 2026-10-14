"""Pure completion-gate evaluation for COA records (FR6).

The completion gate is the single place that decides whether a COA may leave the
system: a COA whose required lab/test fields are not all populated with a passing
result must not be downloadable and must not be printable.

Everything in this module is pure. It performs no I/O, opens no connection and
reads no environment, so the same COA record always yields the same
:class:`ValidationOutcome`. That property is what makes the gate usable from the
HTTP layer (which has already fetched the record) and from unit tests (which
never need a Mule API) without diverging in behaviour.

The gate accepts either shape of COA:

* a domain object with attributes (for example
  :class:`src.api.mule.coa_service.COARecord`), or
* a plain mapping using the contract field names (``coa_id``/``coa_number``).

Required test fields are resolved in this order:

1. an explicit ``required_fields`` argument to :meth:`CompletionGate.validate_coa`
   or to the constructor;
2. the names listed in ``test_results["missing"]``, when the upstream system
   already published them;
3. otherwise every entry of ``test_results["fields"]`` whose value is ``None`` or
   whose ``status`` is anything other than ``"pass"``.

A required field the record never reports as passing counts as missing: the gate
fails closed so an absent test result can never be mistaken for a passing one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "COMPLETE",
    "INCOMPLETE",
    "CompletionGate",
    "ValidationOutcome",
]

#: ``completion_status`` values reported by a released COA.
COMPLETE = "complete"
#: ``completion_status`` values reported by a COA that must not be released.
INCOMPLETE = "incomplete"

#: Upstream completion statuses that mean "the COA finished successfully".
_COMPLETED_STATUS_VALUES = frozenset({"completed", "complete"})

#: ``test_results`` entry statuses that mean the test passed.
_PASSING_STATUS = "pass"


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of evaluating one COA against the completion gate.

    Attributes:
        coa_id: Identifier of the evaluated COA (empty when the record has none).
        completion_status: ``"complete"`` when releasable, else ``"incomplete"``.
        releasable: True only when the COA may be downloaded and printed.
        missing_fields: Names of the required test fields that are not satisfied.
    """

    coa_id: str
    completion_status: str
    releasable: bool
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping used by the HTTP contract."""
        return {
            "coa_id": self.coa_id,
            "completion_status": self.completion_status,
            "releasable": self.releasable,
            "missing_fields": list(self.missing_fields),
        }


def _read(source: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an object, returning ``None`` when absent."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _coa_id_of(coa: Any) -> str:
    """Return the COA identifier regardless of the record shape in use."""
    for field_name in ("coa_id", "coa_number", "id"):
        value = _read(coa, field_name)
        if value:
            return str(value)
    return ""


def _completion_value(coa: Any) -> str | None:
    """Return the normalised upstream completion status of ``coa`` (or ``None``)."""
    raw = _read(coa, "completion_status")
    if raw is None:
        raw = _read(coa, "status")
    if raw is None:
        return None
    # ``CompletionStatus`` is a ``str`` Enum, so ``str(...)`` would render
    # "CompletionStatus.PENDING"; the ``value`` attribute is the wire string.
    value = getattr(raw, "value", raw)
    text = str(value).strip().lower()
    return text or None


def _entry_is_missing(entry: Any) -> bool:
    """Decide whether one ``test_results["fields"]`` entry counts as satisfied."""
    if entry is None:
        return True
    if isinstance(entry, Mapping):
        if "status" in entry:
            status = entry.get("status")
            return status is None or str(status).strip().lower() != _PASSING_STATUS
        if "value" in entry:
            return entry.get("value") is None
        return not entry  # an empty mapping carries no result at all
    status = entry.status if hasattr(entry, "status") else None
    if status is not None:
        return str(status).strip().lower() != _PASSING_STATUS
    if hasattr(entry, "value"):
        return entry.value is None
    return False  # a plain, non-null scalar result is present


def _fields_mapping(test_results: Any) -> Mapping[str, Any]:
    """Return ``test_results["fields"]`` when it is a mapping, else an empty one."""
    fields = _read(test_results, "fields")
    return fields if isinstance(fields, Mapping) else {}


def _declared_missing(test_results: Any) -> list[str] | None:
    """Return the field names published in ``test_results["missing"]`` (or ``None``)."""
    missing = _read(test_results, "missing")
    if missing is None:
        return None
    if isinstance(missing, str):
        return [missing]
    if isinstance(missing, Mapping):
        return [str(name) for name in missing]
    try:
        return [str(name) for name in missing]
    except TypeError:
        return [str(missing)]


def _dedupe(names: Sequence[str]) -> list[str]:
    """Return ``names`` in first-seen order without repeats."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _is_field_missing(test_results: Any, name: str) -> bool:
    """Decide whether the explicitly required field ``name`` is unsatisfied."""
    declared = _declared_missing(test_results)
    if declared is not None and name in declared:
        return True
    fields = _fields_mapping(test_results)
    if name in fields:
        return _entry_is_missing(fields[name])
    # The record never reports this required field, so it cannot prove a pass.
    return True


def _derive_missing_fields(test_results: Any) -> list[str]:
    """Derive the unsatisfied test fields from the record's own ``test_results``."""
    declared = _declared_missing(test_results)
    if declared is not None:
        # Upstream already published the outstanding fields; they are authoritative.
        return _dedupe(declared)
    fields = _fields_mapping(test_results)
    return [str(name) for name, entry in fields.items() if _entry_is_missing(entry)]


class CompletionGate:
    """Evaluate COA completeness without any I/O (FR6).

    Args:
        required_fields: Optional explicit list of test fields every COA must
            satisfy. When omitted the required set is derived from the record's
            ``test_results`` exactly as described in this module's docstring.
    """

    def __init__(self, required_fields: Sequence[str] | None = None) -> None:
        self._required_fields: tuple[str, ...] | None = (
            tuple(str(name) for name in required_fields)
            if required_fields is not None
            else None
        )

    @property
    def required_fields(self) -> tuple[str, ...] | None:
        """The configured required fields, or ``None`` when derived per record."""
        return self._required_fields

    def validate_coa(
        self,
        coa: Any,
        required_fields: Sequence[str] | None = None,
    ) -> ValidationOutcome:
        """Return the :class:`ValidationOutcome` for ``coa``.

        Args:
            coa: A ``COARecord``-like object or a mapping describing one COA.
            required_fields: Per-call override of the configured required fields.

        Raises:
            ValueError: If ``coa`` is ``None``; an absent COA cannot be judged.
        """
        if coa is None:
            raise ValueError("a COA record is required")

        coa_id = _coa_id_of(coa)
        test_results = _read(coa, "test_results")

        explicit = self._resolve_required_fields(required_fields)
        if explicit is not None:
            missing = [name for name in explicit if _is_field_missing(test_results, name)]
        else:
            missing = _derive_missing_fields(test_results)

        status = _completion_value(coa)
        status_is_final = status is None or status in _COMPLETED_STATUS_VALUES
        releasable = not missing and status_is_final

        return ValidationOutcome(
            coa_id=coa_id,
            completion_status=COMPLETE if releasable else INCOMPLETE,
            releasable=releasable,
            missing_fields=missing,
        )

    def validate_coa_id(self, coa_id: str, loader: Callable[[str], Any]) -> ValidationOutcome:
        """Validate the COA returned by ``loader`` for ``coa_id``.

        Args:
            coa_id: Identifier passed to ``loader``.
            loader: Callable returning the record for that identifier, or ``None``
                when the identifier is unknown.

        Returns:
            The gate outcome; an unknown identifier is reported as incomplete
            rather than raising, so callers can map it onto their own 404.
        """
        record = loader(coa_id)
        if record is None:
            return ValidationOutcome(
                coa_id=str(coa_id),
                completion_status=INCOMPLETE,
                releasable=False,
                missing_fields=["coa_record"],
            )
        outcome = self.validate_coa(record)
        if outcome.coa_id:
            return outcome
        return replace(outcome, coa_id=str(coa_id))

    def _resolve_required_fields(
        self, required_fields: Sequence[str] | None
    ) -> tuple[str, ...] | None:
        if required_fields is not None:
            return tuple(str(name) for name in required_fields)
        return self._required_fields