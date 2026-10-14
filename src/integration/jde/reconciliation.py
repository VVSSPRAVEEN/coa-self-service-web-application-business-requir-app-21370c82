"""Reconcile live JDE test state against the RDS field catalog and gate on it (FR6, FR14).

This module is where the completion gate stops trusting the Mule-carried copy of
a COA's test results. Three facts are combined:

1. the live test fields JDE holds (:class:`~src.integration.jde.reader.JDEFieldReader`),
2. the live COA lifecycle status JDE holds (:class:`~src.integration.jde.status.JDECOAStatus`),
3. the *expected* field catalog maintained in RDS
   (:class:`RDSMetadataRepository`).

:func:`reconcile` answers "which expected fields are not satisfied right now?",
and :func:`validate_release` folds that together with the lifecycle status to
produce the very same :class:`~src.validation.completion_gate.validator.ValidationOutcome`
the pure gate produces, so an HTTP handler can prefer the live result without
knowing which of the two paths answered.

One design decision is load-bearing and therefore worth stating: per-field
satisfaction is decided by the *existing* :class:`CompletionGate` rather than by a
second rule written here. ``reconcile`` evaluates a synthetic probe record
through the pure gate with the expected catalog as its explicit required set, so
"a required field the record never reports as passing counts as missing" is
guaranteed to hold identically on the live path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from src.validation.completion_gate import COMPLETE, INCOMPLETE, CompletionGate, ValidationOutcome

from .client import (
    ExpectedFieldProvider,
    JDE_BASE_URL_ENV,
    JDEError,
    JDETransport,
    HttpJSONTransport,
    coerce_field_names,
)
from .reader import JDEFieldReader
from .status import JDECOAStatus, jde_status_is_final

__all__ = [
    "DEFAULT_FIELD_CATALOG_ID",
    "EnvFieldCatalog",
    "ExpectedFieldCatalog",
    "JDEClient",
    "RDSMetadataRepository",
    "ReconciliationResult",
    "build_client_from_env",
    "reconcile",
    "validate_release",
]

#: Catalog consulted when a deployment configures no specific one.
DEFAULT_FIELD_CATALOG_ID = "default"

#: Prefix of the environment variable that configures one catalog entry.
ENV_CATALOG_PREFIX = "JDE_FIELD_CATALOG_"


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of comparing live JDE test fields against the expected catalog.

    Attributes:
        present: Expected field names JDE currently satisfies.
        missing: Expected field names that are absent or not passing.
        extra: Field names JDE reported that the catalog does not require.
    """

    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when no expected field is outstanding, so the release path may proceed."""
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready mapping used by the validate/reconcile reports."""
        return {
            "present": list(self.present),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "complete": self.complete,
        }


class ExpectedFieldCatalog(Protocol):
    """Structural contract of an expected-field catalog provider."""

    def get_expected_fields(self, catalog_id: str) -> list[str]:
        """Return the field names required for ``catalog_id``."""
        ...


def reconcile(jde_fields: Mapping[str, Any], expected: Sequence[str]) -> ReconciliationResult:
    """Compare live JDE test fields against the expected-field catalog.

    A required field is satisfied only when JDE reports it *and* reports it as
    passing; a field JDE never reports counts as missing, so the gate fails
    closed on an absent result.

    Args:
        jde_fields: The normalised live fields from
            :meth:`~src.integration.jde.reader.JDEFieldReader.read_test_fields`.
        expected: The expected field names from the RDS catalog.

    Returns:
        The :class:`ReconciliationResult`. An empty catalog declares no
        requirements, so it reconciles as complete.
    """
    required = coerce_field_names(expected)
    fields = dict(jde_fields or {})

    probe = CompletionGate(required_fields=required).validate_coa(
        {"coa_id": "", "test_results": {"fields": fields}}
    )
    missing = list(probe.missing_fields)
    outstanding = set(missing)
    return ReconciliationResult(
        present=[name for name in required if name not in outstanding],
        missing=missing,
        extra=sorted(str(name) for name in fields if str(name) not in set(required)),
    )


def validate_release(
    jde_status: Mapping[str, Any],
    jde_fields: Mapping[str, Any],
    expected: Sequence[str],
) -> ValidationOutcome:
    """Produce the release verdict from live JDE state (FR6).

    Args:
        jde_status: The normalised status from
            :meth:`~src.integration.jde.status.JDECOAStatus.fetch_coa_status`.
        jde_fields: The normalised live test fields from JDE.
        expected: The expected field names from the RDS catalog.

    Returns:
        A :class:`ValidationOutcome` shaped exactly like the pure gate's, so a
        handler can return it without knowing which path answered. When the COA's
        lifecycle status is not one of the released values, the outcome is
        blocked with an empty ``missing_fields`` - which is what makes the 409
        read "blocked until the upstream completion status is 'completed'".
    """
    reconciliation = reconcile(jde_fields, expected)
    coa_id = str((jde_status or {}).get("coa_id") or "")
    releasable = reconciliation.complete and jde_status_is_final(jde_status)
    return ValidationOutcome(
        coa_id=coa_id,
        completion_status=COMPLETE if releasable else INCOMPLETE,
        releasable=releasable,
        missing_fields=list(reconciliation.missing),
    )


class EnvFieldCatalog:
    """Expected-field catalog backed by the deployment's environment.

    Each catalog is one environment variable, ``JDE_FIELD_CATALOG_<CATALOG_ID>``
    upper-cased and hyphen-normalised, holding a comma-separated list of field
    names. A catalog that is not configured resolves to an empty list, which
    :func:`reconcile` treats as "no required fields"; the deployment is
    responsible for configuring the catalogs it needs.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self._env = os.environ if env is None else env

    def variable_name(self, catalog_id: str) -> str:
        """Return the environment variable that configures ``catalog_id``."""
        suffix = "".join(
            character if character.isalnum() else "_" for character in str(catalog_id).upper()
        )
        return f"{ENV_CATALOG_PREFIX}{suffix}"

    def get_expected_fields(self, catalog_id: str) -> list[str]:
        """Return the configured field names for ``catalog_id``."""
        return coerce_field_names(self._env.get(self.variable_name(catalog_id), ""))


class RDSMetadataRepository:
    """The expected-field catalog kept in RDS, read through an injected provider.

    Args:
        expected_provider: Either a callable taking a catalog id and returning
            the field names, or any object exposing
            ``get_expected_fields(catalog_id)``.

    The provider is injected rather than opened here so this repository can be
    exercised without a database, and so a deployment can swap the RDS client
    for a cached or replicated one without touching the gate.
    """

    def __init__(self, expected_provider: ExpectedFieldProvider | ExpectedFieldCatalog) -> None:
        if expected_provider is None:
            raise ValueError("an expected-field provider is required")
        self._provider = expected_provider

    @property
    def provider(self) -> Any:
        """The injected provider backing this repository."""
        return self._provider

    def get_expected_fields(self, catalog_id: str) -> list[str]:
        """Return the field names ``catalog_id`` requires.

        Args:
            catalog_id: Identifier of the catalog to read.

        Returns:
            The ordered, de-duplicated field names. An unconfigured or unknown
            catalog yields an empty list.

        Raises:
            JDEError: If the provider itself fails.
        """
        key = str(catalog_id or "").strip() or DEFAULT_FIELD_CATALOG_ID
        getter: Optional[Callable[[str], Any]] = getattr(
            self._provider, "get_expected_fields", None
        )
        reader = getter if callable(getter) else self._provider
        try:
            raw = reader(key)
        except JDEError:
            raise
        except Exception as exc:
            raise JDEError(f"the expected-field catalog {key!r} could not be read: {exc}") from exc
        return coerce_field_names(raw)

    def get_expected_fields_or_raise(self, catalog_id: str) -> list[str]:
        """Like :meth:`get_expected_fields`, but an empty catalog is an error.

        Used by the release path when a catalog is mandatory, so a missing
        catalog blocks the release instead of declaring it complete.
        """
        fields = self.get_expected_fields(catalog_id)
        if not fields:
            raise JDEError(
                f"the expected-field catalog {catalog_id!r} is empty; "
                "a COA cannot be released against an unconfigured catalog"
            )
        return fields


class JDEClient:
    """Read live JDE state and turn it into a completion-gate verdict (FR6, FR14).

    This is the facade the HTTP layer holds. It owns the three live reads and
    nothing else, so a handler asks one object for a verdict instead of
    coordinating a transport, a reader, a status reader and a catalog.

    It lives in this module rather than in :mod:`~src.integration.jde.client`
    because that module is the leaf transport and must not import its siblings;
    this module already depends on all of them.

    Args:
        transport: The JDE transport every live read goes through.
        catalog: The expected-field catalog repository.
        catalog_id: The catalog this deployment releases against.
    """

    def __init__(
        self,
        transport: JDETransport,
        catalog: RDSMetadataRepository,
        *,
        catalog_id: str = DEFAULT_FIELD_CATALOG_ID,
    ) -> None:
        self._reader = JDEFieldReader(transport)
        self._status = JDECOAStatus(transport)
        self._catalog = catalog
        self._catalog_id = str(catalog_id or DEFAULT_FIELD_CATALOG_ID)

    @property
    def catalog_id(self) -> str:
        """The expected-field catalog this client reconciles against."""
        return self._catalog_id

    def read_test_fields(self, field_id: str) -> dict[str, Any]:
        """Read the live test fields for ``field_id`` out of JDE."""
        return self._reader.read_test_fields(field_id)

    def fetch_coa_status(self, coa_id: str) -> dict[str, Any]:
        """Read the live lifecycle status for ``coa_id`` out of JDE."""
        return self._status.fetch_coa_status(coa_id)

    def expected_fields(self, catalog_id: Optional[str] = None) -> list[str]:
        """Return the expected field names for ``catalog_id`` or the configured one."""
        return self._catalog.get_expected_fields(catalog_id or self._catalog_id)

    def release_outcome(self, coa_id: str) -> ValidationOutcome:
        """Read all three live facts and return the release verdict for ``coa_id``.

        Raises:
            JDEError: If any of the three live reads fails.
        """
        status = self._status.fetch_coa_status(coa_id)
        fields = self._reader.read_test_fields(coa_id)
        expected = self._catalog.get_expected_fields(self._catalog_id)
        return validate_release(status, fields, expected)

    @classmethod
    def from_env(
        cls,
        *,
        env: Optional[Mapping[str, str]] = None,
        catalog_id: Optional[str] = None,
        transport: Optional[JDETransport] = None,
    ) -> "JDEClient":
        """Build the client from the deployment's environment.

        Args:
            env: Environment mapping to read; defaults to ``os.environ``.
            catalog_id: Catalog override; defaults to ``JDE_FIELD_CATALOG_ID``
                and then to :data:`DEFAULT_FIELD_CATALOG_ID`.
            transport: Transport override, used by tests and by deployments that
                manage their own HTTP stack.

        Raises:
            JDEConfigurationError: If no transport is injected and
                ``JDE_BASE_URL`` is unset, so live JDE state is unavailable.
        """
        source = os.environ if env is None else env
        resolved_transport = transport or HttpJSONTransport.from_env(env=source)
        resolved_catalog = RDSMetadataRepository(EnvFieldCatalog(source))
        return cls(
            resolved_transport,
            resolved_catalog,
            catalog_id=str(
                (source.get("JDE_FIELD_CATALOG_ID") or "").strip() or catalog_id or DEFAULT_FIELD_CATALOG_ID
            ),
        )


def build_client_from_env() -> Optional[JDEClient]:
    """Return a :class:`JDEClient`, or ``None`` when JDE is not configured.

    ``JDE_BASE_URL`` decides: when it is unset the deployment has deliberately not
    integrated JDE, and the caller falls back to the Mule-carried record. A
    *configured* JDE that then fails to answer raises :class:`JDEError` and must
    be surfaced by the caller rather than silently degrading.
    """
    if not (os.environ.get(JDE_BASE_URL_ENV) or "").strip():
        return None
    return JDEClient.from_env()
