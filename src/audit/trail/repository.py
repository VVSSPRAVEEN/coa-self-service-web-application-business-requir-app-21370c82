"""Append-only repository interface for audit log storage (FR12).

Two storage backends satisfy :class:`AuditStorageBackend`:

* :class:`PostgresAuditStorage` - the durable store. Rows live in the
  ``audit_events`` table declared by ``migrations/001_initial_schema.sql``:
  hash-chained, append-only, and read back in chain order. This is what a
  deployment runs, and it is what :func:`build_audit_storage` selects whenever a
  database DSN is configured.
* :class:`InMemoryAuditStorage` - the **documented fallback** for tests and
  local development, pinned explicitly with ``P3_PERSISTENCE_BACKEND=memory``.
  It is no longer the silent default of a configured deployment.

:func:`build_audit_storage` is the factory :class:`AuditRepository` goes through,
so a caller that constructs ``AuditRepository()`` - including
``src.api.mule.coa_service`` and the audit CLI - is durable whenever the
deployment says so, without any of them learning which backend they got.

**Column mapping.** ``sequence_number`` is the dense, zero-based chain position
that :mod:`src.audit.trail.chain` verifies, and is stored explicitly rather than
being inferred from the bigint surrogate; ``external_event_id`` keeps the model's
``event_id`` verbatim (an idempotency key is not always a UUID); ``details`` and
``ip_address`` are the two remaining model fields. The canonical payload the hash
was computed over is persisted in ``payload_json`` so the digest can be rechecked
against the original bytes.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from src.audit.trail.models import AuditEventType, AuditLogEntry
from src.core.postgres import (
    BACKEND_POSTGRES,
    connection,
    resolve_backend,
    resolve_dsn,
)

__all__ = [
    "AuditRepository",
    "AuditStorageBackend",
    "InMemoryAuditStorage",
    "PostgresAuditStorage",
    "build_audit_storage",
]

#: The ``audit_events`` columns this storage reads and writes, in the order every
#: ``SELECT`` list uses. Positional DB-API rows are mapped through this tuple, so
#: the order is part of the contract.
_ENTRY_COLUMNS: Tuple[str, ...] = (
    "external_event_id",
    "event_type",
    "actor_id",
    "coa_id",
    "occurred_at",
    "prev_hash",
    "entry_hash",
    "sequence_number",
    "details",
    "ip_address",
)

_SELECT_LIST = ", ".join(_ENTRY_COLUMNS)

#: Namespaced UUID for an ``event_id`` that is not itself a UUID (an idempotency
#: key). Deterministic, so replaying the same key yields the same ``event_id``
#: column value instead of a fresh random one.
_NON_UUID_EVENT_NAMESPACE = uuid.UUID("6f2b5f1e-0d3a-5c47-9c2e-6a1b0d4f8e31")

#: Advisory-lock key guarding chain-position assignment. Any fixed 64-bit value
#: works; the constant keeps concurrent writers from minting the same position.
_CHAIN_ADVISORY_LOCK = 7_284_119_003


class AuditStorageBackend(Protocol):
    """Protocol for pluggable storage backends."""

    def append(self, entry: AuditLogEntry) -> None:
        ...

    def get_all(self) -> List[AuditLogEntry]:
        ...

    def get_by_resource(self, resource_id: str) -> List[AuditLogEntry]:
        ...

    def get_by_type(self, event_type: AuditEventType) -> List[AuditLogEntry]:
        ...

    def get_older_than(self, cutoff: datetime) -> List[AuditLogEntry]:
        ...

    def count(self) -> int:
        ...


class InMemoryAuditStorage:
    """Thread-safe in-memory append-only storage for testing and local dev.

    No update or delete operations are exposed, enforcing immutability. Selected
    only when no database DSN is configured, or when
    ``P3_PERSISTENCE_BACKEND=memory`` pins it explicitly.
    """

    def __init__(self) -> None:
        self._entries: List[AuditLogEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: AuditLogEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def get_all(self) -> List[AuditLogEntry]:
        with self._lock:
            return list(self._entries)

    def get_by_resource(self, resource_id: str) -> List[AuditLogEntry]:
        with self._lock:
            return [e for e in self._entries if e.resource_id == resource_id]

    def get_by_type(self, event_type: AuditEventType) -> List[AuditLogEntry]:
        with self._lock:
            return [e for e in self._entries if e.event_type == event_type]

    def get_older_than(self, cutoff: datetime) -> List[AuditLogEntry]:
        with self._lock:
            return [e for e in self._entries if e.timestamp < cutoff]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)


def _row_values(row: Any) -> Dict[str, Any]:
    """Normalise a DB-API row into a ``{column: value}`` mapping."""
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(_ENTRY_COLUMNS, row))


def _as_utc(value: Any) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    psycopg2 returns ``TIMESTAMPTZ`` as an aware ``datetime``; a hand-written
    test row may carry an ISO-8601 string. The digest was computed over an aware
    timestamp, so a naive value is pinned to UTC rather than left ambiguous.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _row_to_entry(row: Any) -> AuditLogEntry:
    """Map one ``audit_events`` row onto :class:`AuditLogEntry`."""
    values = _row_values(row)
    details = values.get("details")
    ip_address = values.get("ip_address")
    return AuditLogEntry(
        event_id=str(values["external_event_id"]),
        event_type=AuditEventType(str(values["event_type"])),
        user_sub=str(values["actor_id"]),
        resource_id=str(values["coa_id"]),
        details=None if details is None else str(details),
        ip_address=None if ip_address is None else str(ip_address),
        timestamp=_as_utc(values["occurred_at"]),
        previous_hash=str(values["prev_hash"]),
        entry_hash=str(values["entry_hash"]),
        sequence_number=int(values["sequence_number"]),
    )


def _event_id_column(event_id: str) -> str:
    """Return the ``event_id`` UUID column value for a model ``event_id``.

    ``audit_events.event_id`` is a UUID, but a model ``event_id`` may be an
    idempotency key chosen by a caller. Keys that parse are used as-is; anything
    else is folded into a deterministic UUID5 so the non-null column is honoured
    without losing the original value, which ``external_event_id`` keeps.

    The result is a string rather than a :class:`uuid.UUID` because psycopg2 only
    adapts UUID objects after ``psycopg2.extras.register_uuid()`` has been
    called; a string is accepted by a uuid column without that registration.
    """
    try:
        return str(uuid.UUID(str(event_id)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_NON_UUID_EVENT_NAMESPACE, str(event_id)))


class PostgresAuditStorage:
    """Durable, hash-chained audit storage on the ``audit_events`` table.

    Args:
        dsn_or_conn: A PostgreSQL DSN, or an already-open DB-API connection. An
            injected connection is neither committed nor closed - its lifetime
            belongs to the caller - which is how the unit suite drives this class
            against a recording fake.

    Immutability is structural, not conventional: like
    :class:`InMemoryAuditStorage`, this class exposes no update or delete, and
    the table's unique indexes on ``entry_hash`` and ``sequence_number`` make a
    forked chain impossible to write.
    """

    def __init__(self, dsn_or_conn: Any) -> None:
        self._target = dsn_or_conn

    @staticmethod
    def _next_sequence(cursor: Any) -> int:
        """Return the next dense chain position under a transaction lock."""
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_ADVISORY_LOCK,))
        cursor.execute(
            "SELECT COALESCE(MAX(sequence_number), -1) + 1 AS next_sequence "
            "FROM audit_events"
        )
        row = cursor.fetchone()
        return int(row["next_sequence"] if isinstance(row, Mapping) else row[0])

    def append(self, entry: AuditLogEntry) -> None:
        """Insert one sealed entry.

        The chain position is assigned by the database rather than trusted from
        the caller, so two concurrent writers cannot mint the same
        ``sequence_number`` and fork the chain. The entry's own ``previous_hash``
        is stored verbatim: the hash chain is what makes the trail tamper-evident,
        so this store records rather than rewrites it.
        """
        payload = {
            "canonical_payload": entry.compute_canonical_payload(),
            "event_id": entry.event_id,
            "event_type": entry.event_type.value,
            "details": entry.details,
            "ip_address": entry.ip_address,
            "sequence_number": entry.sequence_number,
        }
        with connection(self._target) as conn:
            with conn.cursor() as cursor:
                sequence = self._next_sequence(cursor)
                cursor.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, external_event_id, event_type, actor_id, coa_id,
                        occurred_at, payload_json, prev_hash, entry_hash,
                        sequence_number, details, ip_address
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                    """,
                    (
                        _event_id_column(entry.event_id),
                        entry.event_id,
                        entry.event_type.value,
                        entry.user_sub,
                        entry.resource_id,
                        entry.timestamp,
                        json.dumps(payload, default=str),
                        entry.previous_hash,
                        entry.entry_hash,
                        sequence,
                        entry.details,
                        entry.ip_address,
                    ),
                )

    def _query(self, where: str, params: Sequence[Any]) -> List[AuditLogEntry]:
        """Run a filtered read in chain order and map every row."""
        with connection(self._target) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_LIST} FROM audit_events {where} "
                    f"ORDER BY sequence_number",
                    tuple(params),
                )
                rows = cursor.fetchall() or []
        return [_row_to_entry(row) for row in rows]

    def get_all(self) -> List[AuditLogEntry]:
        """Return the whole trail in chain order."""
        return self._query("", ())

    def get_by_resource(self, resource_id: str) -> List[AuditLogEntry]:
        """Return every entry recorded against ``resource_id``, in chain order."""
        return self._query("WHERE coa_id = %s", (resource_id,))

    def get_by_type(self, event_type: AuditEventType) -> List[AuditLogEntry]:
        """Return every entry of ``event_type``, in chain order."""
        value = getattr(event_type, "value", event_type)
        return self._query("WHERE event_type = %s", (str(value),))

    def get_older_than(self, cutoff: datetime) -> List[AuditLogEntry]:
        """Return every entry older than ``cutoff``, in chain order."""
        return self._query("WHERE occurred_at < %s", (cutoff,))

    def count(self) -> int:
        """Return the total number of entries in the trail."""
        with connection(self._target) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT count(*) AS entry_count FROM audit_events")
                row = cursor.fetchone()
        return int(row["entry_count"] if isinstance(row, Mapping) else row[0])


def build_audit_storage(
    env: Optional[Mapping[str, str]] = None,
) -> AuditStorageBackend:
    """Return the audit storage backend this process should use.

    Args:
        env: Environment mapping to read; :data:`os.environ` by default.

    Returns:
        :class:`PostgresAuditStorage` when a DSN is configured (via
        :data:`~src.core.postgres.DSN_ENV_VARS`), otherwise
        :class:`InMemoryAuditStorage` - the documented in-memory fallback that
        keeps the chain unit-testable without a database.

    Raises:
        PersistenceConfigurationError: If ``P3_PERSISTENCE_BACKEND=postgres`` is
            pinned but no DSN is configured.
    """
    if resolve_backend(env) != BACKEND_POSTGRES:
        return InMemoryAuditStorage()
    dsn = resolve_dsn(env)
    assert dsn is not None  # resolve_backend already rejected the empty case
    return PostgresAuditStorage(dsn)


class AuditRepository:
    """Repository wrapping an append-only storage backend.

    Enforces that no update or delete path exists.
    """

    def __init__(self, storage: Optional[AuditStorageBackend] = None) -> None:
        self._storage: AuditStorageBackend = storage or build_audit_storage()

    def append(self, entry: AuditLogEntry) -> None:
        """Append an entry to the audit log. This is the only write path."""
        self._storage.append(entry)

    def get_all(self) -> List[AuditLogEntry]:
        """Retrieve all entries in insertion order."""
        return self._storage.get_all()

    def get_by_resource(self, resource_id: str) -> List[AuditLogEntry]:
        """Retrieve all entries for a specific resource."""
        return self._storage.get_by_resource(resource_id)

    def get_by_type(self, event_type: AuditEventType) -> List[AuditLogEntry]:
        """Retrieve all entries of a specific event type."""
        return self._storage.get_by_type(event_type)

    def get_older_than(self, cutoff: datetime) -> List[AuditLogEntry]:
        """Retrieve entries older than the given cutoff datetime."""
        return self._storage.get_older_than(cutoff)

    def count(self) -> int:
        """Return the total number of entries."""
        return self._storage.count()
