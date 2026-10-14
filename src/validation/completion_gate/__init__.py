"""COA completion gate: completeness evaluation and release policy (FR6).

This package owns exactly one public surface, the gate, split into two modules:

* :mod:`.validator` owns :class:`CompletionGate` and :class:`ValidationOutcome`,
  the pure "is this COA complete?" evaluation.
* :mod:`.release_policy` owns :class:`ReleasePolicy`, :class:`GateDecision` and
  :class:`CompletionGateError`, the operational "may it be released?" decision
  and the typed failure the HTTP layer maps to ``409``.

Every name exported here is defined by the module it is imported from, so
importing this package can never raise ``ImportError`` for a missing submodule.
"""

from .release_policy import CompletionGateError, GateDecision, ReleasePolicy
from .validator import COMPLETE, INCOMPLETE, CompletionGate, ValidationOutcome

__all__ = [
    "COMPLETE",
    "INCOMPLETE",
    "CompletionGate",
    "CompletionGateError",
    "GateDecision",
    "ReleasePolicy",
    "ValidationOutcome",
]
