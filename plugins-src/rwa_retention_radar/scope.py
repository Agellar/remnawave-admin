"""Fail-closed user visibility for Retention Radar requests.

The plugin works with cached panel users directly, so coarse plugin RBAC is
not enough: regular admins must also inherit the panel's per-user visibility
contract.  ``None`` deliberately keeps its core meaning (unrestricted), while
an empty set means that the request may not see any users.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any


def _provider():
    # Lazy import keeps plugin discovery independent from the shared database
    # bootstrap and gives tests a narrow seam for resolver failures.
    from shared.rbac import get_visible_user_uuids

    return get_visible_user_uuids


def _normalise(values: Iterable[Any]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("user scope must be an iterable of UUIDs")

    visible: set[str] = set()
    for value in values:
        try:
            visible.add(str(uuid.UUID(str(value).strip())).lower())
        except (AttributeError, TypeError, ValueError):
            # Dropping a malformed allowance is fail-closed; accepting it or
            # failing the SQL cast would be less safe.
            continue
    return frozenset(visible)


async def resolve_visible_user_uuids(ctx: Any, admin: Any) -> frozenset[str] | None:
    """Resolve the authenticated admin's visible users.

    Legacy environment admins and superadmins keep unrestricted behaviour.
    Every error for an account-backed regular admin returns an empty set, never
    ``None``, so an RBAC/database outage cannot widen access.
    """
    account_id = getattr(admin, "account_id", None)
    role = getattr(admin, "role", None)
    if account_id is None or role == "superadmin":
        return None

    try:
        resolved = await _provider()(account_id, role)
        if resolved is None:
            return None
        return _normalise(resolved)
    except Exception:  # noqa: BLE001 -- authorization must fail closed
        ctx.logger.warning(
            "retention_radar.user_scope_failed",
            extra={"admin_account_id": account_id},
            exc_info=True,
        )
        return frozenset()
