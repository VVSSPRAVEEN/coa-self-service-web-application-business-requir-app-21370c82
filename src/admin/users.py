'''Administrator-managed external accounts (the ``/admin/users`` registry).

This module is the single owner of the portal's external-account registry. It
exposes four operations - listing, reading, creating and re-roling an account -
over two interchangeable stores:

* :class:`PostgresUserRegistry` - the durable store. Rows live in the
  ``admin_users`` table declared by ``migrations/001_initial_schema.sql``, and it
  is what :func:`_store` returns whenever a database DSN is configured. Every
  statement it emits is plain SQL against that table's columns.
* :class:`JsonUserRegistry` - the JSON-document store, selected only when
  ``P3_PERSISTENCE_BACKEND=memory`` pins it (which the compose ``unit-tests``
  service does deliberately, so the suite can exercise the registry with no
  database server). A configured deployment never receives it.

:func:`_store` is the factory the public functions go through, so ``list_users``,
``get_user``, ``create_external_account`` and ``update_role`` behave identically
whichever store is in use. Roles are validated in the public functions before any
store is touched, so this registry remains the single source of truth for which
roles may be granted - on either store.

Design notes:

* The JSON store's path is read from ``ADMIN_USERS_PATH`` on every call (default
  ``./data/admin_users.json``), so a deployment or a test can repoint it without
  re-importing this module.
* Every read-modify-write on the JSON store runs under one module-level
  re-entrant lock and is persisted write-temp-then-replace, so a concurrent
  reader never observes a half-written document.
* ``created_at`` is an ISO-8601 string on :class:`UserDTO` for both stores. The
  PostgreSQL store renders ``TIMESTAMPTZ`` as UTC ISO-8601, which sorts
  identically to the JSON store's strings, so ``list_users`` ordering matches.
* A ``username`` collision reported by the unique index as SQLSTATE ``23505`` is
  translated into :class:`DuplicateUserError`, so a caller sees the same typed
  failure whichever store raised it.
* Nothing in this module performs network access.
'''

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from src.core.postgres import (
    BACKEND_POSTGRES,
    connection,
    resolve_backend,
    resolve_dsn,
)

__all__ = [
    'ALLOWED_ROLES',
    'DEFAULT_STORE_PATH',
    'DuplicateUserError',
    'InvalidRoleError',
    'JsonUserRegistry',
    'PostgresUserRegistry',
    'UserDTO',
    'UserNotFoundError',
    'UserRegistry',
    'create_external_account',
    'get_user',
    'list_users',
    'update_role',
]

#: Roles an administrator may grant to an external account.
ALLOWED_ROLES = ('internal', '3pl', 'customer')

#: Store path used by the in-memory backend when ``ADMIN_USERS_PATH`` is unset.
DEFAULT_STORE_PATH = './data/admin_users.json'

#: Email syntax accepted by the registry. Deliberately the same shape the shared
#: ``src.core.models`` annotation enforces, re-implemented locally so this module
#: keeps no pydantic dependency.
_EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')

#: Human-readable role list used in :class:`InvalidRoleError` messages.
_ALLOWED_ROLES_TEXT = ', '.join(ALLOWED_ROLES)

_LOCK = threading.RLock()

#: The ``admin_users`` columns this module reads and writes, in the order every
#: ``SELECT``/``RETURNING`` list uses. Positional DB-API rows are mapped through
#: this tuple, so the order is part of the contract.
_USER_COLUMNS = (
    'user_id',
    'username',
    'email',
    'role',
    'organization',
    'created_at',
)

_SELECT_LIST = ', '.join(_USER_COLUMNS)
_SELECT_ALL_USERS_SQL = "SELECT " + _SELECT_LIST + " FROM admin_users"
_SELECT_ONE_USER_SQL = "SELECT " + _SELECT_LIST + " FROM admin_users WHERE user_id = %s"
_SELECT_ROLE_UPDATE_SQL = "UPDATE admin_users SET role = %s WHERE user_id = %s RETURNING " + _SELECT_LIST

#: SQLSTATE 23505 - unique_violation, raised by admin_users_username_key.
_UNIQUE_VIOLATION = '23505'


class DuplicateUserError(Exception):
    '''Raised when an account with the same username already exists.

    Attributes:
        username: The username that already exists.
    '''

    def __init__(self, username: str) -> None:
        super().__init__(f'a user with username {username!r} already exists')
        self.username = username


class UserNotFoundError(Exception):
    '''Raised when no account matches the requested identifier.

    Attributes:
        user_id: The identifier that was not found.
    '''

    def __init__(self, user_id: str) -> None:
        super().__init__(f'no user with user_id {user_id!r}')
        self.user_id = user_id


class InvalidRoleError(ValueError):
    '''Raised when a role outside :data:`ALLOWED_ROLES` is requested.

    Subclasses ``ValueError`` so a caller that only cares about bad input can
    catch the base type.
    '''

    def __init__(self, role: Any) -> None:
        super().__init__(f'role must be one of {_ALLOWED_ROLES_TEXT}; got {role!r}')
        self.role = role


@dataclass(frozen=True)
class UserDTO:
    '''One external account as returned by the registry.'''

    user_id: str
    username: str
    email: str | None
    role: str
    organization: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        '''Return the JSON-ready mapping used by the HTTP contract.'''
        return asdict(self)


class UserRegistry(Protocol):
    '''The store contract the public registry functions depend on.'''

    def list_users(self) -> list[UserDTO]:
        ...

    def get_user(self, user_id: str) -> UserDTO | None:
        ...

    def create(self, row: Mapping[str, Any]) -> UserDTO:
        ...

    def update_role(self, user_id: str, role: str) -> UserDTO | None:
        ...


def _store_path() -> Path:
    '''Return the configured in-memory store path.'''
    return Path(os.getenv('ADMIN_USERS_PATH', DEFAULT_STORE_PATH))


def _validate_role(role: Any) -> str:
    '''Return the normalised role, raising :class:`InvalidRoleError` when unknown.'''
    if not isinstance(role, str):
        raise InvalidRoleError(role)
    normalized = role.strip()
    if normalized not in ALLOWED_ROLES:
        raise InvalidRoleError(role)
    return normalized


def _validate_username(username: Any) -> str:
    '''Return the trimmed username, raising ``ValueError`` when it is unusable.'''
    if not isinstance(username, str) or not username.strip():
        raise ValueError('username must be a non-empty string')
    return username.strip()


def _validate_email(email: Any) -> str | None:
    '''Return the trimmed email address, raising ``ValueError`` when malformed.'''
    if email is None or email == '':
        return None
    if not isinstance(email, str):
        raise TypeError('email must be a string')
    trimmed = email.strip()
    if not _EMAIL_PATTERN.match(trimmed):
        raise ValueError(f'email is not a valid address: {email!r}')
    return trimmed


def _validate_organization(organization: Any) -> str | None:
    '''Return the trimmed organization name, or ``None`` when absent.'''
    if organization is None:
        return None
    if not isinstance(organization, str):
        raise TypeError('organization must be a string')
    trimmed = organization.strip()
    return trimmed or None


def _read_rows() -> list[dict[str, Any]]:
    '''Read every stored account row; a missing or corrupt file reads as empty.'''
    path = _store_path()
    try:
        raw = path.read_text(encoding='utf-8')
    except (FileNotFoundError, OSError):
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(row) for row in parsed if isinstance(row, Mapping)]


def _write_rows(rows: Iterable[Mapping[str, Any]]) -> None:
    '''Persist ``rows`` atomically, creating the store directory when needed.'''
    path = _store_path()
    if str(path.parent) not in ('', '.'):
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([dict(row) for row in rows], indent=2, sort_keys=True)
    temp_path = path.with_name(f'{path.name}.tmp')
    temp_path.write_text(payload + '\n', encoding='utf-8')
    os.replace(temp_path, path)


def _values(row: Any) -> dict[str, Any]:
    '''Normalise one stored row into a ``{column: value}`` mapping.

    Accepts either a mapping (a JSON document row or a ``RealDictCursor`` row)
    or a positional sequence, which is mapped through :data:`_USER_COLUMNS`.
    '''
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip(_USER_COLUMNS, row))


def _iso_utc(value: Any) -> str:
    '''Render a ``TIMESTAMPTZ`` value as a UTC ISO-8601 string.

    psycopg2 hands back an aware ``datetime``; a JSON-document row carries a
    string. Both render to the same shape, so ``list_users`` ordering is
    identical on either store.
    '''
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    return str(value)


def _row_to_dto(row: Any) -> UserDTO:
    '''Map one stored account row - JSON document or DB-API row - onto :class:`UserDTO`.'''
    values = _values(row)
    email = values.get('email')
    organization = values.get('organization')
    return UserDTO(
        user_id=str(values.get('user_id', '')),
        username=str(values.get('username', '')),
        email=None if email is None else str(email),
        role=str(values.get('role', '')),
        organization=None if organization is None else str(organization),
        created_at=_iso_utc(values.get('created_at', '')),
    )


def _sorted_users(users: list[UserDTO]) -> list[UserDTO]:
    '''Order by creation time then username, the registry's documented order.'''
    return sorted(users, key=lambda dto: (dto.created_at, dto.username))


def _is_unique_violation(exc: BaseException) -> bool:
    '''Return ``True`` when ``exc`` is a PostgreSQL unique-constraint failure.'''
    sqlstate = getattr(exc, 'pgcode', None)
    if sqlstate is None:
        sqlstate = getattr(getattr(exc, 'diag', None), 'sqlstate', None)
    return sqlstate == _UNIQUE_VIOLATION


class JsonUserRegistry:
    '''JSON-document registry, selected only when the backend is pinned to memory.

    Every mutation is serialised by :data:`_LOCK` and written atomically. The
    durable, deployment-facing store is :class:`PostgresUserRegistry`.
    '''

    def list_users(self) -> list[UserDTO]:
        '''Return every registered account in creation order.'''
        with _LOCK:
            return _sorted_users([_row_to_dto(row) for row in _read_rows()])

    def get_user(self, user_id: str) -> UserDTO | None:
        '''Return the account with ``user_id``, or ``None`` when it is unknown.'''
        with _LOCK:
            for row in _read_rows():
                if str(row.get('user_id')) == str(user_id):
                    return _row_to_dto(row)
        return None

    def create(self, row: Mapping[str, Any]) -> UserDTO:
        '''Append one validated row, rejecting a case-insensitive duplicate.'''
        username = str(row['username'])
        with _LOCK:
            rows = _read_rows()
            for existing in rows:
                same = str(existing.get('username', '')).strip().lower()
                if same == username.lower():
                    raise DuplicateUserError(username)
            rows.append(dict(row))
            _write_rows(rows)
        return _row_to_dto(row)

    def update_role(self, user_id: str, role: str) -> UserDTO | None:
        '''Set ``role`` on the matching row, or return ``None`` when unknown.'''
        with _LOCK:
            rows = _read_rows()
            for index, existing in enumerate(rows):
                if str(existing.get('user_id')) == str(user_id):
                    updated = dict(existing)
                    updated['role'] = role
                    rows[index] = updated
                    _write_rows(rows)
                    return _row_to_dto(updated)
        return None


class PostgresUserRegistry:
    '''Durable registry backed by the ``admin_users`` table.

    Args:
        dsn_or_conn: A PostgreSQL DSN, or an already-open DB-API connection. An
            injected connection is neither committed nor closed, which is how the
            unit suite drives this class against a recording fake.

    ``migrations/001_initial_schema.sql`` declares the shape this class relies
    on: ``user_id``/``username`` are unique and ``role`` is restricted to
    :data:`ALLOWED_ROLES` by a CHECK constraint, so a role this registry rejects
    is one the database rejects too. Every statement is issued through the
    shared :func:`~src.core.postgres.connection` helper, which commits on success
    and closes only a connection this class opened.
    '''

    def __init__(self, dsn_or_conn: Any) -> None:
        self._target = dsn_or_conn

    def list_users(self) -> list[UserDTO]:
        '''Return every registered account, oldest first.'''
        with connection(self._target) as conn, conn.cursor() as cursor:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- statement is a fixed module-level constant and every value is a bound %s parameter
            cursor.execute(_SELECT_ALL_USERS_SQL)
            rows = cursor.fetchall() or []
        return _sorted_users([_row_to_dto(row) for row in rows])

    def get_user(self, user_id: str) -> UserDTO | None:
        '''Return the account with ``user_id``, or ``None`` when it is unknown.'''
        with connection(self._target) as conn, conn.cursor() as cursor:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- statement is a fixed module-level constant and every value is a bound %s parameter
            cursor.execute(
                _SELECT_ONE_USER_SQL,
                (user_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _row_to_dto(row)

    def create(self, row: Mapping[str, Any]) -> UserDTO:
        '''Insert one validated row and return it as read back from the database.'''
        columns = list(row.keys())
        placeholders = ', '.join(['%s'] * len(columns))
        params = tuple(row[column] for column in columns)
        username = str(row.get('username', ''))
        with connection(self._target) as conn, conn.cursor() as cursor:
            cursor.execute(
                'SELECT 1 FROM admin_users WHERE lower(username) = lower(%s)',
                (username,),
            )
            if cursor.fetchone() is not None:
                raise DuplicateUserError(username)
            try:
                cursor.execute(
                    f'INSERT INTO admin_users ({", ".join(columns)}) '
                    f'VALUES ({placeholders}) RETURNING {_SELECT_LIST}',
                    params,
                )
            except Exception as exc:  # translated, re-raised below
                if _is_unique_violation(exc):
                    raise DuplicateUserError(str(row.get('username', ''))) from exc
                raise
            created = cursor.fetchone()
        if created is None:  # INSERT ... RETURNING always yields one row.
            raise RuntimeError('admin_users INSERT ... RETURNING produced no row')
        return _row_to_dto(created)

    def update_role(self, user_id: str, role: str) -> UserDTO | None:
        '''Set ``role`` on the matching row, or return ``None`` when unknown.'''
        with connection(self._target) as conn, conn.cursor() as cursor:
            cursor.execute(
                f'UPDATE admin_users SET role = %s WHERE user_id = %s '
                f'RETURNING {_SELECT_LIST}',
                (role, user_id),
            )
            updated = cursor.fetchone()
        return None if updated is None else _row_to_dto(updated)


def _store() -> UserRegistry:
    '''Return the registry this deployment is configured to use.

    A configured database DSN yields the durable
    :class:`PostgresUserRegistry`; the in-memory backend pinned by
    ``P3_PERSISTENCE_BACKEND=memory`` yields :class:`JsonUserRegistry`.
    '''
    dsn = resolve_dsn()
    if resolve_backend() == BACKEND_POSTGRES and dsn:
        return PostgresUserRegistry(dsn)
    return JsonUserRegistry()


def list_users() -> list[UserDTO]:
    '''Return every external account an administrator may grant roles to.'''
    return _store().list_users()


def get_user(user_id: str) -> UserDTO | None:
    '''Return one external account, or ``None`` when the identifier is unknown.'''
    return _store().get_user(user_id)


def create_external_account(
    payload: Any,
    email: str | None = None,
    role: str = 'internal',
    organization: str | None = None,
    *,
    user_id: str | None = None,
) -> UserDTO:
    '''Create one external account after validating every field.

    Args:
        username: Unique login name; required.
        email: Optional contact address, validated against the local syntax.
        role: One of :data:`ALLOWED_ROLES`; defaults to ``internal``.
        organization: Optional tenant the external account belongs to.
        user_id: Optional caller-supplied identifier; a UUID hex string is minted
            when it is omitted.

    Raises:
        ValueError: When a field is unusable.
        InvalidRoleError: When ``role`` is outside :data:`ALLOWED_ROLES`.
        DuplicateUserError: When ``username`` already exists on the chosen store.
    '''
    if isinstance(payload, Mapping):
        # The HTTP contract hands the validated body over as one mapping, while
        # direct callers use the keyword form; both are first-class.
        data = dict(payload)
        username = data.get('username')
        email = data.get('email', email)
        role = data.get('role', role)
        organization = data.get('organization', organization)
        if user_id is None:
            user_id = data.get('user_id')
    else:
        username = payload
    row = {
        'user_id': user_id or uuid.uuid4().hex,
        'username': _validate_username(username),
        'email': _validate_email(email),
        'role': _validate_role(role),
        'organization': _validate_organization(organization),
        'created_at': _iso_utc(datetime.now(UTC)),
    }
    return _store().create(row)


def update_role(user_id: str, role: str) -> UserDTO:
    '''Grant ``role`` to the account identified by ``user_id``.

    Raises:
        InvalidRoleError: When ``role`` is outside :data:`ALLOWED_ROLES`.
        UserNotFoundError: When no account carries ``user_id``.
    '''
    normalized = _validate_role(role)
    updated = _store().update_role(user_id, normalized)
    if updated is None:
        raise UserNotFoundError(user_id)
    return updated
