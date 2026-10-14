"""Persistence boundary for administrator-managed sister-plant mappings (FR9).

A deployment stores every mapping in PostgreSQL.
:class:`PostgresPlantMappingRepository` is what
:func:`build_plant_mapping_repository` returns whenever a database DSN is
configured - which is the case for the compose ``api``, ``worker`` and
``migrate`` services, all of which export the same
``postgresql://coa:...@audit-db:5432/coa_audit`` value - so the mappings an
administrator creates through the plant-mapping router are durable and survive a
restart. Every statement it emits is plain SQL against the ``plant_mappings``
table declared by ``migrations/001_initial_schema.sql``.

:class:`PlantMappingRepository` satisfies the identical protocol from a
process-local dict. It is selected only when ``P3_PERSISTENCE_BACKEND=memory``
pins it, which the compose ``unit-tests`` service exports so the domain rules can
be exercised with no database server. Both implementations hand back the same
:class:`~src.admin.plant_mapping.models.PlantMapping` objects through the same
:class:`PlantMappingStore` methods, so the service, the router and the tests are
unchanged whichever one is in use.

**Threading.** :class:`~src.admin.plant_mapping.service.PlantMappingService` is
async but the DB-API driver is not, so :class:`PostgresPlantMappingRepository`
keeps a small *synchronous* core (``create_sync`` and friends) and exposes each
one as an ``async def`` that hands the blocking call to a worker thread with
:func:`asyncio.to_thread`. The public surface is therefore identical for both
implementations, and the service - which never learns which one it holds - is
unchanged.

**Column mapping.** ``sister_plant_id`` is stored in the migration's
``target_plant_id`` column and ``is_active`` in its ``active`` column: they are
the same fact, so the mapping is done here rather than by duplicating a column.
``mapping_uuid`` is the stable identifier every caller addresses a mapping by;
the table's bigint ``id`` stays an internal surrogate used for insertion order.

**Duplicates.** ``migrations/001_initial_schema.sql`` declares the partial unique
index ``plant_mappings_active_pair_key``, so at most one active mapping may exist
per ``(source_plant_id, target_plant_id)`` pair. The domain-level
:class:`~src.admin.plant_mapping.service.DuplicateMappingError` is raised by the
service from its own pre-check; this module adds no second rule and simply lets
the index reject a racing concurrent insert.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from src.admin.plant_mapping.models import (
    PlantMapping,
    PlantMappingCreate,
    PlantMappingUpdate,
)
from src.core.postgres import (
    BACKEND_POSTGRES,
    connection,
    resolve_backend,
    resolve_dsn,
)

__all__ = [
    "PlantMappingRepository",
    "PlantMappingStore",
    "PostgresPlantMappingRepository",
    "build_plant_mapping_repository",
]


#: The ``plant_mappings`` columns this repository reads and writes, in the order
#: every ``SELECT``/``RETURNING`` list uses. Positional DB-API rows are mapped
#: through this tuple, so the order is part of the contract.
_MAPPING_COLUMNS: tuple[str, ...] = (
    "mapping_uuid",
    "source_plant_id",
    "target_plant_id",
    "active",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)

_SELECT_LIST = ", ".join(_MAPPING_COLUMNS)


class PlantMappingStore(Protocol):
    """The persistence contract :class:`PlantMappingService` depends on.

    Both :class:`PostgresPlantMappingRepository` and
    :class:`PlantMappingRepository` satisfy it, which is what lets the service stay
    unaware of which store a deployment runs.
    """

    async def create(self, payload: PlantMappingCreate) -> PlantMapping:
        ...

    async def get_by_id(self, mapping_id: str) -> PlantMapping | None:
        ...

    async def list_all(self) -> list[PlantMapping]:
        ...

    async def get_by_source_plant(self, source_plant_id: str) -> list[PlantMapping]:
        ...

    async def update(
        self, mapping_id: str, payload: PlantMappingUpdate
    ) -> PlantMapping | None:
        ...

    async def delete(self, mapping_id: str) -> bool:
        ...


def _new_mapping_id() -> str:
    """Mint an identifier in the model's own id format.

    Both stores call this so a mapping created with the backend pinned to memory
    carries the same identifier shape the PostgreSQL store writes into
    ``plant_mappings.mapping_uuid``.
    """
    factory = getattr(PlantMapping.model_fields["id"], "default_factory", None)
    if factory is None:
        return uuid.uuid4().hex
    return str(factory())


def _row_values(row: Any) -> dict[str, Any]:
    """Normalise a DB-API row into a ``{column: value}`` mapping.

    Accepts either a mapping (a ``RealDictCursor`` row, or a canned row from a
    test) or a positional sequence, which is mapped through
    :data:`_MAPPING_COLUMNS`.
    """
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(_MAPPING_COLUMNS, row))


def _as_utc(value: Any) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    psycopg2 returns ``TIMESTAMPTZ`` as an aware ``datetime`` and ``TIMESTAMP``
    as a naive one; a hand-written row may carry an ISO-8601 string. All three are
    accepted so the reconstructed model never trips pydantic.
    """
    if isinstance(value, datetime):
        return (
            value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        )
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return (
            parsed
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=UTC)
        )
    return datetime.now(UTC)


def _row_to_mapping(row: Any) -> PlantMapping:
    """Map one ``plant_mappings`` row onto :class:`PlantMapping`."""
    values = _row_values(row)
    updated_by = values.get("updated_by")
    return PlantMapping(
        id=str(values["mapping_uuid"]),
        source_plant_id=str(values["source_plant_id"]),
        sister_plant_id=str(values["target_plant_id"]),
        is_active=bool(values["active"]),
        created_by=str(values["created_by"]),
        updated_by=str(updated_by) if updated_by is not None else "",
        created_at=_as_utc(values["created_at"]),
        updated_at=_as_utc(values["updated_at"]),
    )


def _fetch_all(cursor: Any, sql: str, params: Sequence[Any]) -> list[PlantMapping]:
    """Run ``sql`` and map every returned row."""
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall() or []
    return [_row_to_mapping(row) for row in rows]


def _fetch_one(
    cursor: Any, sql: str, params: Sequence[Any]
) -> PlantMapping | None:
    """Run ``sql`` and map the single returned row, or ``None`` when there is none."""
    cursor.execute(sql, tuple(params))
    row = cursor.fetchone()
    return None if row is None else _row_to_mapping(row)


class PlantMappingRepository:
    """Process-local mapping store used only when the backend is pinned to memory.

    Selected by :func:`build_plant_mapping_repository` when
    ``P3_PERSISTENCE_BACKEND=memory`` is exported - which the compose
    ``unit-tests`` service does so the domain rules run with no database server.
    Every deployment that configures a DSN gets
    :class:`PostgresPlantMappingRepository` and durable rows instead.
    """

    def __init__(self) -> None:
        self._store: dict[str, PlantMapping] = {}
        self._order: list[str] = []

    async def create(self, payload: PlantMappingCreate) -> PlantMapping:
        now = datetime.now(UTC)
        mapping = PlantMapping(
            id=_new_mapping_id(),
            source_plant_id=payload.source_plant_id,
            sister_plant_id=payload.sister_plant_id,
            is_active=True,
            created_by=payload.created_by,
            updated_by=payload.created_by,
            created_at=now,
            updated_at=now,
        )
        self._store[mapping.id] = mapping
        self._order.append(mapping.id)
        return mapping

    async def get_by_id(self, mapping_id: str) -> PlantMapping | None:
        return self._store.get(mapping_id)

    async def list_all(self) -> list[PlantMapping]:
        return [
            self._store[key]
            for key in self._order
            if key in self._store
        ]

    async def get_by_source_plant(self, source_plant_id: str) -> list[PlantMapping]:
        return [
            mapping
            for mapping in await self.list_all()
            if mapping.source_plant_id == source_plant_id
        ]

    async def update(
        self, mapping_id: str, payload: PlantMappingUpdate
    ) -> PlantMapping | None:
        existing = self._store.get(mapping_id)
        if existing is None:
            return None
        if payload.sister_plant_id is not None:
            existing.sister_plant_id = payload.sister_plant_id
        if payload.is_active is not None:
            existing.is_active = payload.is_active
        existing.updated_by = payload.updated_by
        existing.updated_at = datetime.now(UTC)
        self._store[mapping_id] = existing
        return existing

    async def delete(self, mapping_id: str) -> bool:
        if mapping_id in self._store:
            del self._store[mapping_id]
            self._order = [key for key in self._order if key != mapping_id]
            return True
        return False


class PostgresPlantMappingRepository:
    """Durable FR9 mapping store backed by the ``plant_mappings`` table.

    Args:
        dsn_or_conn: A PostgreSQL DSN, or an already-open DB-API connection. An
            injected connection is neither committed nor closed - its lifetime
            belongs to the caller - which is how the unit suite drives this class
            against a recording fake.

    Every ``async`` method delegates to the identically named ``*_sync`` core on
    a worker thread, so the service's public API is unchanged whether this class
    or :class:`PlantMappingRepository` is in use. Each ``*_sync`` method opens (or
    borrows) a connection through :func:`~src.core.postgres.connection`, which
    commits on success and closes a connection this class opened.
    """

    def __init__(self, dsn_or_conn: str | Any) -> None:
        self._target = dsn_or_conn

    # -- synchronous core ----------------------------------------------------

    def create_sync(self, payload: PlantMappingCreate) -> PlantMapping:
        """Insert one mapping and return it as read back from the database."""
        now = datetime.now(UTC)
        with connection(self._target) as conn, conn.cursor() as cursor:
            mapping = _fetch_one(
                cursor,
                f"""
                    INSERT INTO plant_mappings (
                        mapping_uuid, source_plant_id, target_plant_id, active,
                        created_by, updated_by, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_SELECT_LIST}
                    """,
                (
                    # The model's own id factory, so both stores mint the same
                    # identifier format.
                    _new_mapping_id(),
                    payload.source_plant_id,
                    payload.sister_plant_id,
                    True,
                    payload.created_by,
                    payload.created_by,
                    now,
                    now,
                ),
            )
        if mapping is None:  # INSERT ... RETURNING always yields one row.
            raise RuntimeError("plant_mappings INSERT ... RETURNING produced no row")
        return mapping

    def get_by_id_sync(self, mapping_id: str) -> PlantMapping | None:
        """Return the mapping with ``mapping_uuid``, or ``None`` when unknown."""
        with connection(self._target) as conn, conn.cursor() as cursor:
            return _fetch_one(
                cursor,
                f"SELECT {_SELECT_LIST} FROM plant_mappings "
                f"WHERE mapping_uuid = %s",
                (mapping_id,),
            )

    def list_all_sync(self) -> list[PlantMapping]:
        """Return every mapping, in insertion order."""
        with connection(self._target) as conn, conn.cursor() as cursor:
            return _fetch_all(
                cursor,
                f"SELECT {_SELECT_LIST} FROM plant_mappings ORDER BY id",
                (),
            )

    def get_by_source_plant_sync(self, source_plant_id: str) -> list[PlantMapping]:
        """Return every mapping declared for ``source_plant_id``."""
        with connection(self._target) as conn, conn.cursor() as cursor:
            return _fetch_all(
                cursor,
                f"SELECT {_SELECT_LIST} FROM plant_mappings "
                f"WHERE source_plant_id = %s ORDER BY id",
                (source_plant_id,),
            )

    def update_sync(
        self, mapping_id: str, payload: PlantMappingUpdate
    ) -> PlantMapping | None:
        """Apply the non-null fields of ``payload`` to one mapping row."""
        with connection(self._target) as conn, conn.cursor() as cursor:
            return _fetch_one(
                cursor,
                f"""
                    UPDATE plant_mappings
                       SET target_plant_id = COALESCE(%s, target_plant_id),
                           active          = COALESCE(%s, active),
                           updated_by      = %s,
                           updated_at      = %s
                     WHERE mapping_uuid = %s
                    RETURNING {_SELECT_LIST}
                    """,
                (
                    payload.sister_plant_id,
                    payload.is_active,
                    payload.updated_by,
                    datetime.now(UTC),
                    mapping_id,
                ),
            )

    def delete_sync(self, mapping_id: str) -> bool:
        """Delete one mapping row, returning whether a row was removed."""
        with connection(self._target) as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM plant_mappings WHERE mapping_uuid = %s "
                "RETURNING mapping_uuid",
                (mapping_id,),
            )
            return cursor.fetchone() is not None

    # -- async surface -------------------------------------------------------

    async def create(self, payload: PlantMappingCreate) -> PlantMapping:
        """Insert one mapping without blocking the event loop."""
        return await asyncio.to_thread(self.create_sync, payload)

    async def get_by_id(self, mapping_id: str) -> PlantMapping | None:
        """Read one mapping without blocking the event loop."""
        return await asyncio.to_thread(self.get_by_id_sync, mapping_id)

    async def list_all(self) -> list[PlantMapping]:
        """Read every mapping without blocking the event loop."""
        return await asyncio.to_thread(self.list_all_sync)

    async def get_by_source_plant(self, source_plant_id: str) -> list[PlantMapping]:
        """Read one source plant's mappings without blocking the event loop."""
        return await asyncio.to_thread(self.get_by_source_plant_sync, source_plant_id)

    async def update(
        self, mapping_id: str, payload: PlantMappingUpdate
    ) -> PlantMapping | None:
        """Update one mapping without blocking the event loop."""
        return await asyncio.to_thread(self.update_sync, mapping_id, payload)

    async def delete(self, mapping_id: str) -> bool:
        """Delete one mapping without blocking the event loop."""
        return await asyncio.to_thread(self.delete_sync, mapping_id)


def build_plant_mapping_repository(
    env: Mapping[str, str] | None = None,
) -> PlantMappingStore:
    """Return the durable store, or the memory-pinned fallback.

    The backend is resolved from ``env`` (the process environment by default):
    the ``auto`` default answers ``postgres`` when a DSN is configured and
    ``memory`` when none is, and ``P3_PERSISTENCE_BACKEND=memory`` pins the
    in-memory store explicitly.

    Args:
        env: Environment mapping to resolve the backend and DSN from; the
            process environment when omitted.
    """
    if resolve_backend(env) == BACKEND_POSTGRES:
        return PostgresPlantMappingRepository(resolve_dsn(env))
    return PlantMappingRepository()
