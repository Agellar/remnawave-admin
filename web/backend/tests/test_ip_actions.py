"""RBAC and access-scope checks for IP-trial Telegram actions."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import ip_actions


def _callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.from_user.first_name = "Operator"
    callback.from_user.id = 42
    callback.answer = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_users_action_requires_users_view_permission():
    callback = _callback("ipact:users:91.201.236.46")
    admin = MagicMock()
    admin.has_permission = AsyncMock(return_value=False)

    with patch.object(ip_actions, "_show_users", AsyncMock()) as show_users:
        await ip_actions.handle_ip_action(callback, admin)

    admin.has_permission.assert_awaited_once_with("users", "view")
    callback.answer.assert_awaited_once_with("Недостаточно прав", show_alert=True)
    show_users.assert_not_awaited()


@pytest.mark.asyncio
async def test_users_action_filters_admin_user_scope():
    callback = _callback("ipact:users:91.201.236.46")
    admin = MagicMock()
    admin.get_visible_user_uuids = AsyncMock(return_value={"visible-uuid"})
    database = MagicMock()
    database.get_shared_ip_accounts = AsyncMock(return_value=[{
        "ip": "91.201.236.46",
        "users": [
            {"uuid": "visible-uuid", "username": "visible-user", "conns": 3, "is_active": True},
            {"uuid": "hidden-uuid", "username": "hidden-user", "conns": 9, "is_active": True},
        ],
    }])

    with patch.object(ip_actions, "db_service", database):
        await ip_actions._show_users(callback, "91.201.236.46", admin)

    text = callback.message.answer.await_args.args[0]
    assert "visible-user" in text
    assert "hidden-user" not in text
