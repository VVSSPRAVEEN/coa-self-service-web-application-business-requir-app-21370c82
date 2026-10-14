"""Role-based access control for the COA portal.

One table, one rule: a request is allowed when **any** role held by the caller
grants the requested ``(resource, action)`` pair. The table is deliberately
small and explicit so the authorisation surface can be read in one screen:

======== ========= ==========================================================
Role     Scope     Grants
======== ========= ==========================================================
internal all       every resource and every action, including administration
3pl     subset     ``coas`` (read), ``downloads`` (read/download),
                   ``prints`` (read/print), ``reports`` (read/report)
customer subset    ``coas`` (read), ``downloads`` (read/download)
======== ========= ==========================================================

Any role the table does not know grants nothing, and any action a resource does
not define is denied, so the policy fails closed as it is extended.

Callers may pass a :class:`src.auth.oidc.User`, any object exposing a ``roles``
attribute, a mapping carrying ``roles``, or a bare role string; all four are
normalised to the same comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ACTIONS",
    "ALL_RESOURCES",
    "RESOURCE_ACTIONS",
    "ROLE_RESOURCES",
    "authorize",
    "get_allowed_resources",
]

#: Actions the contract can authorise.
ACTIONS = frozenset({"read", "download", "print", "report", "admin"})

#: Actions each resource accepts.
RESOURCE_ACTIONS: Mapping[str, frozenset[str]] = {
    "coas": frozenset({"read", "download"}),
    "downloads": frozenset({"read", "download"}),
    "prints": frozenset({"read", "print"}),
    "reports": frozenset({"read", "report"}),
    "admin": frozenset({"admin"}),
}

#: Resources reachable by each role. ``internal`` reaches all of them.
ROLE_RESOURCES: Mapping[str, frozenset[str]] = {
    "internal": frozenset(RESOURCE_ACTIONS),
    "3pl": frozenset({"coas", "downloads", "prints", "reports"}),
    "customer": frozenset({"coas", "downloads"}),
}

#: Union of every resource in the table; the grant of the ``internal`` role.
ALL_RESOURCES = frozenset(RESOURCE_ACTIONS)


def _roles_of(user: Any) -> list[str]:
    """Normalise ``user`` into the list of roles it holds."""
    if user is None:
        return []
    raw: Any
    if isinstance(user, str):
        raw = [user]
    elif isinstance(user, Mapping):
        raw = user.get("roles")
    else:
        raw = getattr(user, "roles", None)
        if raw is None and getattr(user, "role", None) is not None:
            raw = [user.role]
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: Iterable[Any] = [raw]
    elif isinstance(raw, Iterable):
        candidates = raw
    else:
        candidates = [raw]
    return [
        str(getattr(role, "value", role)).strip().lower()
        for role in candidates
        if str(getattr(role, "value", role)).strip()
    ]


def _normalize_action(action: Any) -> str | None:
    """Return the canonical action name, or ``None`` when it is not an action."""
    if not isinstance(action, str):
        return None
    normalized = action.strip().lower()
    return normalized or None


def get_allowed_resources(user: Any) -> set[str]:
    """Return every resource at least one of ``user``'s roles can reach."""
    allowed: set[str] = set()
    for role in _roles_of(user):
        allowed.update(ROLE_RESOURCES.get(role, frozenset()))
    return allowed


def authorize(user: Any, resource: str, action: str) -> bool:
    """Return True when ``user`` may perform ``action`` on ``resource``.

    Args:
        user: A ``User``, an object with ``roles``, a mapping with ``roles`` or a
            bare role string.
        resource: Resource name from :data:`RESOURCE_ACTIONS`.
        action: Action name from :data:`ACTIONS`.

    Returns:
        ``True`` when at least one held role grants the pair. Unknown roles,
        unknown resources and unknown actions all return ``False``.
    """
    if not isinstance(resource, str) or not resource.strip():
        return False
    normalized_action = _normalize_action(action)
    if normalized_action is None:
        return False

    normalized_resource = resource.strip().lower()
    resource_actions = RESOURCE_ACTIONS.get(normalized_resource)
    if resource_actions is None or normalized_action not in resource_actions:
        return False

    for role in _roles_of(user):
        if normalized_resource in ROLE_RESOURCES.get(role, frozenset()):
            return True
    return False
