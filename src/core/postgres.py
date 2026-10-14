"""Shared PostgreSQL wiring for the durable repositories (FR2/FR9/FR12/FR13).

Three persistence boundaries in this backend - the FR9 sister-plant mapping
store, the FR1/FR2 external-account registry and the FR12 audit trail - all need
the same three things, and each used to answer them with a comment:

1. *Which database?* :func:`resolve_dsn` reads the deployment's DSN out of the
   environment, consulting :data:`DSN_ENV_VARS` in order. The compose
   ``migrate`` service, the ``api`` service and the ``worker`` service all pass
   the same ``postgresql://coa:...@audit-db:5432/coa_audit`` value, so the
   repositories and the one-shot migration always address the same database.

2. *Durable or in-memory?* :func:`resolve_backend` turns that DSN into the
   backend identifier. ``auto`` (the default) selects ``postgres`` when a DSN is
   configured and ``memory`` when it is not; ``P3_PERSISTENCE_BACKEND`` pins the
   answer either way. The override exists so the in-memory implementations stay
   usable as an **explicit** test/dev fallback - the compose ``unit-tests``
   service sets it to ``memory`` precisely because that suite drives the
   repositories against in-process doubles and must not inherit the api
   service's DSN.

3. *How do I get a cursor?* :func:`connection` accepts either a DSN or an
   already-open DB-API connection. A DSN opens a real ``psycopg2`` connection,
   commits on success and closes it again; an object that already exposes
   ``cursor()`` is used as-is and left open, because the caller owns its
   lifetime. That second form is what lets ``tests/unit/test_postgres_repositories.py``
   exercise every statement this backend layer emits against a recording fake
   without a database server.

``psycopg2`` is imported lazily inside :func:`_import_psycopg2`, never at module
scope, so importing this module (and therefore any repository that depends on it)
stays free in an environment where the driver is not installed. Every statement
this backend layer emits is plain SQL against the tables declared in
``migrations/001_initial_schema.sql``; there is no ORM and no query builder.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

__all__ = [
    "BACKEND_AUTO",
    "BACKEND_ENV",
    "BACKEND_MEMORY",
    "BACKEND_POSTGRES",
    "DSN_ENV_VARS",
    "PersistenceConfigurationError",
    "PostgresDriverUnavailableError",
    "connection",
    "resolve_backend",
    "resolve_dsn",
]

#: Environment variable that pins the persistence backend regardless of DSN.
#: One of ``auto`` (default), ``postgres`` or ``memory``.
BACKEND_ENV = "P3_PERSISTENCE_BACKEND"

#: Environment variables consulted, in order, for the audit database DSN.
#: ``P3_AUDIT_DSN`` is the name the compose ``migrate``/``api``/``worker``
#: services use; ``DATABASE_URL`` is the conventional alias and is kept second
#: so an existing deployment that only exports the conventional name keeps
#: working unchanged.
DSN_ENV_VARS = ("P3_AUDIT_DSN", "DATABASE_URL")

#: Durable, PostgreSQL-backed persistence.
BACKEND_POSTGRES = "postgres"

#: Documented in-memory/JSON fallback for tests and local development.
BACKEND_MEMORY = "memory"

#: Let the DSN decide: PostgreSQL when a DSN is configured, memory otherwise.
BACKEND_AUTO = "auto"

_BACKEND_CHOICES = (BACKEND_AUTO, BACKEND_POSTGRES, BACKEND_MEMORY)


class PersistenceConfigurationError(RuntimeError):
    """The backend was pinned to ``postgres`` but no DSN is configured."""


class PostgresDriverUnavailableError(RuntimeError):
    """``psycopg2`` is configured as the driver but is not importable."""


def _env(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Read ``name`` from ``env`` (default :data:`os.environ`), trimmed."""
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def resolve_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured audit-database DSN, or ``None`` when unset.

    Args:
        env: Environment mapping to read; :data:`os.environ` by default.

    Returns:
        The first non-empty value among :data:`DSN_ENV_VARS`, or ``None``.
    """
    for name in DSN_ENV_VARS:
        value = _env(name, env)
        if value:
            return value
    return None


def resolve_backend(env: Mapping[str, str] | None = None) -> str:
    """Return the persistence backend this process should use.

    Args:
        env: Environment mapping to read; :data:`os.environ` by default.

    Returns:
        :data:`BACKEND_POSTGRES` or :data:`BACKEND_MEMORY`.

    Raises:
        ValueError: If :data:`BACKEND_ENV` names an unknown backend.
        PersistenceConfigurationError: If ``postgres`` is pinned but no DSN is
            configured, rather than silently degrading to the in-memory store.
    """
    requested = (_env(BACKEND_ENV, env) or BACKEND_AUTO).lower()
    if requested not in _BACKEND_CHOICES:
        raise ValueError(
            f"{BACKEND_ENV} must be one of {', '.join(_BACKEND_CHOICES)}; got {requested!r}"
        )
    if requested == BACKEND_AUTO:
        return BACKEND_POSTGRES if resolve_dsn(env) else BACKEND_MEMORY
    if requested == BACKEND_POSTGRES and not resolve_dsn(env):
        raise PersistenceConfigurationError(
            f"{BACKEND_ENV}=postgres requires a DSN in "
            f"{' or '.join(DSN_ENV_VARS)}; neither is set"
        )
    return requested


def _import_psycopg2() -> Any:
    """Import ``psycopg2`` on demand, with an actionable error when it is absent."""
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise PostgresDriverUnavailableError(
            "psycopg2 is required for the durable persistence backend; install "
            "psycopg2-binary or set P3_PERSISTENCE_BACKEND=memory to use the "
            "in-memory fallback"
        ) from exc
    return psycopg2


@contextmanager
def connection(dsn_or_conn: Any) -> Iterator[Any]:
    """Yield a DB-API connection for ``dsn_or_conn``.

    An object that already exposes ``cursor()`` is used as-is and is neither
    committed nor closed - the caller owns its lifetime, which is what allows a
    test to inject a recording fake. A DSN string opens a real connection,
    commits when the block exits normally, rolls back when it raises, and
    closes the connection either way.

    Args:
        dsn_or_conn: A DSN string, or an open DB-API connection.

    Yields:
        The connection to issue statements on.
    """
    if hasattr(dsn_or_conn, "cursor"):
        yield dsn_or_conn
        return

    psycopg2 = _import_psycopg2()
    conn = psycopg2.connect(str(dsn_or_conn))
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001  # pragma: no cover - rollback is best effort
            __import__("logging").getLogger(__name__).warning("rollback failed during cleanup", exc_info=True)
        raise
    finally:
        conn.close()