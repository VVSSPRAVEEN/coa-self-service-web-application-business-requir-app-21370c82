"""Single-use tracking for issued presigned download scopes (FR18).

FR18 states that a download URL "cannot be reused after use". A presigned S3 URL
is a bearer credential that S3 itself cannot revoke, so the platform has to carry
the single-use obligation itself: it issues a *scope* alongside the URL, binds
that scope to the requesting actor, and records - in one place - whether the
scope is still live, already spent, or superseded.

**Enforcement boundary.** This store is the server-side enforcement point: it is
consulted by the distribution endpoints that issue scopes. Enforcing it at the
S3 object level - refusing a second GET of an already-consumed URL - requires a
proxy or an S3 policy in front of the bucket; the store cannot do that alone.

State lives in process memory and is guarded by one lock, so concurrent requests
in this process always agree on whether a scope is still usable. It is
deliberately not persisted: a restart drops outstanding scopes, which fails
closed (a URL issued before the restart is no longer active here) rather than
open.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable

__all__ = [
    "SingleUseUrlStore",
    "build_strategy_key",
    "get_consumption_store",
    "reset_consumption_store",
]


def build_strategy_key(coa_id: str, actor: str) -> str:
    """Return the consumption key for one ``(COA, actor)`` pair.

    FR18's "cannot be reused after use" is scoped to the document and the actor
    that obtained it, so a second request from the same actor for the same COA
    supersedes the first request's scope while leaving every other actor's scope
    for that COA untouched.

    Args:
        coa_id: Identifier of the COA being distributed.
        actor: Identifier of the requesting user, or the service account name
            when the caller did not identify itself.

    Returns:
        The opaque store key.
    """
    return f"{str(coa_id).strip()}|{str(actor).strip()}"


class SingleUseUrlStore:
    """Track which issued download scopes are still consumable (FR18).

    The store holds three facts per scope: the strategy key it was issued
    against, whether it has been consumed, and whether it is still the current
    scope for that key. Issuing a scope for a key retires the previous one, so a
    superseding request is the only one that can still be spent.

    Args:
        id_factory: Callable producing scope identifiers; injectable so a test can
            make them deterministic.
    """

    def __init__(self, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(18))
        self._lock = threading.RLock()
        self._active: dict[str, str] = {}
        self._consumed: set[str] = set()

    def issue(self, strategy_key: str) -> str:
        """Issue a fresh scope for ``strategy_key``, retiring any prior scope.

        Args:
            strategy_key: The ``(COA, actor)`` key this scope belongs to; see
                :func:`build_strategy_key`.

        Returns:
            The new scope identifier.
        """
        key = str(strategy_key)
        scope_id = str(self._id_factory())
        with self._lock:
            self._active[key] = scope_id
            self._consumed.discard(scope_id)
        return scope_id

    def consume(self, scope_id: str) -> bool:
        """Spend ``scope_id``.

        Returns:
            True the first time an active scope is spent and False for every
            later call, for a scope that was never issued, and for a scope that
            a later issue for the same key already retired.
        """
        scope = str(scope_id)
        with self._lock:
            if scope in self._consumed or scope not in self._active.values():
                return False
            self._consumed.add(scope)
            return True

    def is_active(self, scope_id: str) -> bool:
        """Return True while ``scope_id`` is still the live scope for its key."""
        scope = str(scope_id)
        with self._lock:
            if scope in self._consumed:
                return False
            return scope in self._active.values()

    def is_consumed(self, scope_id: str) -> bool:
        """Return True once ``scope_id`` has been spent."""
        with self._lock:
            return str(scope_id) in self._consumed

    def active_scope_for(self, strategy_key: str) -> str | None:
        """Return the live scope for ``strategy_key``, or ``None`` when it has none."""
        with self._lock:
            return self._active.get(str(strategy_key))

    def retire(self, scope_id: str) -> bool:
        """Drop ``scope_id`` from the store entirely.

        Returns:
            True when the scope was live and is now gone.
        """
        scope = str(scope_id)
        with self._lock:
            retired = False
            for key, current in list(self._active.items()):
                if current == scope:
                    del self._active[key]
                    retired = True
            self._consumed.discard(scope)
            return retired

    def clear(self) -> None:
        """Drop every scope; used to isolate a test from a shared process store."""
        with self._lock:
            self._active.clear()
            self._consumed.clear()

    def active_scopes(self) -> dict[str, str]:
        """Return a snapshot of ``{strategy_key: live scope_id}``."""
        with self._lock:
            return dict(self._active)


_STORE_LOCK = threading.Lock()
_STORE: SingleUseUrlStore | None = None


def get_consumption_store() -> SingleUseUrlStore:
    """Return the process-wide single-use store, creating it on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SingleUseUrlStore()
        return _STORE


def reset_consumption_store() -> SingleUseUrlStore:
    """Drop the process-wide store and return a fresh, empty one.

    Production code never calls this; it exists so a test can start from a known
    state without leaking scopes into the next test.
    """
    global _STORE
    with _STORE_LOCK:
        _STORE = SingleUseUrlStore()
        return _STORE
