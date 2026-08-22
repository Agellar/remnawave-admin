"""Fail-closed access-policy adapter for the local Live Flow fork.

The upstream plugin checks coarse RBAC actions, but its queries are global.
remnawave-admin also has node access policies and per-admin user visibility;
this module resolves both once per request and translates visible user UUIDs
to the numeric Panel IDs kept by the in-memory poller.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


def _normalise(values: Iterable[Any] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(str(value).strip().lower() for value in values if str(value).strip())


@dataclass(frozen=True)
class AccessScope:
    """Resolved visibility for one authenticated admin.

    ``None`` means unrestricted.  An empty set means deny all.  This is
    deliberately distinct so a failed resolver can fail closed without
    accidentally becoming an unrestricted request.
    """

    node_uuids: frozenset[str] | None
    user_uuids: frozenset[str] | None
    panel_user_ids: frozenset[str] | None
    privileged: bool = False

    @classmethod
    def unrestricted(cls) -> AccessScope:
        return cls(node_uuids=None, user_uuids=None, panel_user_ids=None, privileged=True)

    @classmethod
    def deny_all(cls) -> AccessScope:
        return cls(node_uuids=frozenset(), user_uuids=frozenset(), panel_user_ids=frozenset())

    @property
    def users_restricted(self) -> bool:
        return self.user_uuids is not None

    def allows_node(self, node_uuid: str | None) -> bool:
        if self.node_uuids is None:
            return True
        return bool(node_uuid) and str(node_uuid).lower() in self.node_uuids

    def allows_user_uuid(self, user_uuid: str | None) -> bool:
        if self.user_uuids is None:
            return True
        return bool(user_uuid) and str(user_uuid).lower() in self.user_uuids

    def filter_active(self, users: Iterable[tuple[str, dict]]) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for uid, user in users:
            if self.panel_user_ids is not None and str(uid) not in self.panel_user_ids:
                continue
            if not self.allows_node(user.get("node_uuid")):
                continue
            out.append((str(uid), user))
        return out

    def node_sql_arg(self) -> list[str] | None:
        return None if self.node_uuids is None else sorted(self.node_uuids)

    def user_sql_arg(self) -> list[str] | None:
        return None if self.user_uuids is None else sorted(self.user_uuids)


def _scope_providers():
    # Kept behind a function so imports remain lazy like the official Plugin
    # API and unit tests can replace the providers without a live database.
    from shared.rbac import get_scope, get_visible_user_uuids

    return get_scope, get_visible_user_uuids


async def resolve_access_scope(ctx, admin) -> AccessScope:
    """Resolve node and user visibility, failing closed for regular admins."""
    if getattr(admin, "account_id", None) is None or getattr(admin, "role", None) == "superadmin":
        return AccessScope.unrestricted()

    try:
        get_scope, get_visible_user_uuids = _scope_providers()
        nodes = await get_scope(
            getattr(admin, "account_id", None),
            getattr(admin, "role_id", None),
            getattr(admin, "role", None),
            "node",
            "view",
        )
        users = await get_visible_user_uuids(
            getattr(admin, "account_id", None),
            getattr(admin, "role", None),
        )
        node_uuids = _normalise(nodes)
        user_uuids = _normalise(users)

        if user_uuids is None:
            panel_ids = None
        elif not user_uuids:
            panel_ids = frozenset()
        else:
            rows = await ctx.db.fetch(
                "SELECT id FROM users WHERE uuid = ANY($1::uuid[])",
                sorted(user_uuids),
            )
            panel_ids = frozenset(str(row["id"]) for row in rows if row["id"] is not None)

        return AccessScope(
            node_uuids=node_uuids,
            user_uuids=user_uuids,
            panel_user_ids=panel_ids,
            privileged=False,
        )
    except Exception:  # noqa: BLE001 -- visibility must fail closed
        ctx.logger.warning("live_flow: access scope resolution failed; request denied", exc_info=True)
        return AccessScope.deny_all()
