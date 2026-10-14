"""Runtime job loop for the Ventura COA platform.

The API serves requests; this package owns everything that has to happen on a
timer. Four jobs, each delegating to modules that already exist:

* ``bottomline_ingest`` - drives the existing Bottomline ingestion pipeline
  (:mod:`src.integration.bottomline`) over a local drop directory.
* ``status_sync`` - polls a configured sync endpoint and feeds the payloads
  through the existing webhook API (:mod:`src.integration.sync`).
* ``dlq_redrive`` - replays pending records through the existing dead-letter
  queue (:mod:`src.integration.retry`).
* ``notifications`` - dispatches queued alerts through the existing email
  dispatcher (:mod:`src.notifications.email`).

``src.workers.scheduler`` owns the loop. Public names resolve lazily through
:func:`__getattr__` (PEP 562) rather than being imported here, so
``python -m src.workers.scheduler`` does not import the scheduler module twice -
once as ``src.workers.scheduler`` and once as ``__main__``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "JOB_NAMES",
    "JobOutcome",
    "Scheduler",
    "SystemClock",
    "main",
    "run_once",
]

_EXPORTS: dict[str, tuple[str, ...]] = {
    name: ("scheduler",) for name in __all__
}


def __getattr__(name: str) -> Any:
    """Resolve an exported name from :mod:`src.workers.scheduler` on first access."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".scheduler", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
