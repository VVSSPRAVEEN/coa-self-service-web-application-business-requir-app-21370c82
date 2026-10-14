"""JDE (JD Edwards) integration connector and RDS reconciliation (FR6, FR14).

This package owns every read the platform makes against JD Edwards and the
verdict those reads feed. It is the public surface of the FR14 workstream, so
every name exported below is imported from a submodule that exists in this
package; the package can therefore always be imported and always exposes the
JDE reads and the JDE/RDS reconciliation surface.

* :mod:`~src.integration.jde.client` - the transport seam: the error taxonomy
  (``JDEError`` and friends), the production HTTP/JSON transport
  (``HttpJSONTransport``) and an in-process recording transport
  (``RecordingTransport``) with its ``CoaStatusPath`` / ``TestFieldsPath``
  request descriptions.
* :mod:`~src.integration.jde.reader` - :class:`JDEFieldReader`, the live lab/test
  field results for a COA, plus the ``normalize_field_entry`` /
  ``normalize_field_entries`` normalisers.
* :mod:`~src.integration.jde.status` - :class:`JDECOAStatus`, the live COA
  lifecycle status, with ``COA_COMPLETED_STATUSES`` and
  :func:`jde_status_is_final`.
* :mod:`~src.integration.jde.reconciliation` - :class:`RDSMetadataRepository`,
  :class:`ReconciliationResult`, :func:`reconcile`, :func:`validate_release`,
  the ``EnvFieldCatalog`` / ``DEFAULT_FIELD_CATALOG_ID`` expected-field catalog
  and the :class:`JDEClient` connector facade that binds the live reads to the
  RDS expected-field catalog into one release verdict (also built from the
  environment by :func:`build_client_from_env`).

The dependency order is acyclic and matches the domain order: the transport
knows nothing about meaning, the readers normalise meaning out of a transport,
and the reconciliation module combines the readers with the RDS expected-field
catalog. Consumers that want a single object to read COA fields and COA status
from use :class:`JDEClient`; consumers that reconcile against RDS use
:func:`reconcile` and :func:`validate_release`. Nothing here imports a module
that is not part of this package, so importing it can never raise ``ImportError``
for a missing submodule.
"""

from .client import (
    CoaStatusPath,
    HttpJSONTransport,
    JDEConfigurationError,
    JDEError,
    JDEPayloadError,
    JDETransport,
    JDETransportError,
    RecordedRequest,
    RecordingTransport,
    TestFieldsPath,
)
from .reader import JDEFieldReader, normalize_field_entries, normalize_field_entry
from .reconciliation import (
    DEFAULT_FIELD_CATALOG_ID,
    EnvFieldCatalog,
    JDEClient,
    RDSMetadataRepository,
    ReconciliationResult,
    build_client_from_env,
    reconcile,
    validate_release,
)
from .status import COA_COMPLETED_STATUSES, JDECOAStatus, jde_status_is_final

__all__ = [
    "COA_COMPLETED_STATUSES",
    "CoaStatusPath",
    "DEFAULT_FIELD_CATALOG_ID",
    "EnvFieldCatalog",
    "HttpJSONTransport",
    "JDEClient",
    "JDEConfigurationError",
    "JDECOAStatus",
    "JDEError",
    "JDEFieldReader",
    "JDEPayloadError",
    "JDETransport",
    "JDETransportError",
    "RDSMetadataRepository",
    "RecordedRequest",
    "RecordingTransport",
    "ReconciliationResult",
    "TestFieldsPath",
    "build_client_from_env",
    "jde_status_is_final",
    "normalize_field_entries",
    "normalize_field_entry",
    "reconcile",
    "validate_release",
]
