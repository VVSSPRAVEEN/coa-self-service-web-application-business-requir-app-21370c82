"""Release policy built on top of the pure completion gate (FR6).

The :class:`~src.validation.completion_gate.validator.CompletionGate` answers
"is this COA complete?". The :class:`ReleasePolicy` answers the operational
question the HTTP layer actually asks: "may this COA be released to download or
print, and if not, what exactly is blocking it?".

Both the decision and the typed :class:`CompletionGateError` are defined here so
the transport layer can translate one failure into one status code
(``409`` with ``code="completion_gate_failed"``) without re-deriving the rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .validator import CompletionGate, ValidationOutcome

__all__ = [
    "GateDecision",
    "ReleasePolicy",
    "CompletionGateError",
]


@dataclass(frozen=True)
class GateDecision:
    """Whether a COA may be released, and why."""

    coa_id: str
    allowed: bool
    reason: str
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping for this decision."""
        return {
            "coa_id": self.coa_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
        }


class CompletionGateError(Exception):
    """Raised when a COA is blocked from release by the completion gate.

    Attributes:
        coa_id: Identifier of the blocked COA.
        missing_fields: The required test fields that are still unsatisfied.
    """

    def __init__(
        self,
        message: str,
        *,
        coa_id: str,
        missing_fields: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.coa_id = coa_id
        self.missing_fields: list[str] = list(missing_fields or [])

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping used as the 409 response body."""
        return {
            "detail": str(self),
            "code": "completion_gate_failed",
            "coa_id": self.coa_id,
            "missing_fields": list(self.missing_fields),
        }


class ReleasePolicy:
    """Turn a gate outcome into an allow/deny decision for one COA."""

    #: Reason string returned when the gate is satisfied.
    REASON_RELEASED = "COA is complete and released for download and print"
    #: Reason string returned when required test fields are outstanding.
    REASON_INCOMPLETE = "COA is incomplete; blocked until every required test field is populated"
    #: Reason string returned when the upstream completion status is not final.
    REASON_NOT_COMPLETED = "COA is incomplete; blocked until the upstream completion status is 'completed'"

    def __init__(self, gate: Optional[CompletionGate] = None) -> None:
        self._gate = gate or CompletionGate()

    @property
    def gate(self) -> CompletionGate:
        """The completion gate this policy evaluates."""
        return self._gate

    def evaluate(self, coa: Any) -> GateDecision:
        """Return the release decision for ``coa`` without raising."""
        outcome: ValidationOutcome = self._gate.validate_coa(coa)
        if outcome.releasable:
            return GateDecision(
                coa_id=outcome.coa_id,
                allowed=True,
                reason=self.REASON_RELEASED,
                missing_fields=[],
            )
        reason = (
            self.REASON_INCOMPLETE
            if outcome.missing_fields
            else self.REASON_NOT_COMPLETED
        )
        return GateDecision(
            coa_id=outcome.coa_id,
            allowed=False,
            reason=reason,
            missing_fields=list(outcome.missing_fields),
        )

    def block_on_incomplete(self, coa: Any) -> GateDecision:
        """Return the release decision for ``coa``.

        The name states the effect: while the COA is incomplete the decision is
        ``allowed=False`` and carries the outstanding field names.
        """
        return self.evaluate(coa)

    def require_releasable(self, coa: Any) -> GateDecision:
        """Return the decision, raising :class:`CompletionGateError` when blocked."""
        decision = self.evaluate(coa)
        if decision.allowed:
            return decision
        raise CompletionGateError(
            decision.reason,
            coa_id=decision.coa_id,
            missing_fields=decision.missing_fields,
        )
