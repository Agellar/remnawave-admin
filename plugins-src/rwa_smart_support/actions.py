"""Быстрые действия из отчёта.

Каталог отдаётся фронту, он рисует кнопки и диалоги подтверждения сам —
достаточно вернуть метаданные с i18n-ключами, которые уже есть в локалях
панели (``plugins.smart_support.actions.*``).

Мутации идут через Panel API (``plugin_api.panel_api()``), а не напрямую в
БД: локальная таблица ``users`` — это кэш синхронизации, запись в неё
разъехалась бы с панелью до следующего синка.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

CATALOG: List[Dict[str, Any]] = [
    {
        "id": "reset_traffic",
        "title_i18n": "plugins.smart_support.actions.reset_traffic.title",
        "severity": "safe",
        "requires_confirmation": True,
        "params": [],
    },
    {
        "id": "extend_subscription",
        "title_i18n": "plugins.smart_support.actions.extend_subscription.title",
        "severity": "safe",
        "requires_confirmation": True,
        "params": [
            {
                "name": "days",
                "type": "number",
                "default": 30,
                "label_i18n": "plugins.smart_support.actions.extend_subscription.params.days",
                "min": 1,
                "max": 365,
            }
        ],
    },
    {
        "id": "enable_user",
        "title_i18n": "plugins.smart_support.actions.enable_user.title",
        "severity": "safe",
        "requires_confirmation": True,
        "params": [],
    },
    {
        "id": "disable_user",
        "title_i18n": "plugins.smart_support.actions.disable_user.title",
        "severity": "destructive",
        "requires_confirmation": True,
        "params": [],
    },
    {
        "id": "revoke_subscription",
        "title_i18n": "plugins.smart_support.actions.revoke_subscription.title",
        "severity": "destructive",
        "requires_confirmation": True,
        "params": [
            {
                "name": "revoke_only_passwords",
                "type": "boolean",
                "default": True,
                "label_i18n": (
                    "plugins.smart_support.actions.revoke_subscription."
                    "params.revoke_only_passwords"
                ),
            }
        ],
    },
]

ACTION_IDS = {a["id"] for a in CATALOG}

# Права RBAC: смотреть отчёт и выполнять действия — разные вещи.
RBAC_RESOURCES = {"smart_support": ["view", "execute"]}


async def resolve_panel_user_id(db, user_uuid: str) -> Optional[int]:
    """Числовой id юзера в панели. Панель 3.x адресует юзеров по нему, а не
    по UUID; в локальном кэше он лежит в ``users.id``."""
    panel_id = await db.fetchval("SELECT id FROM users WHERE uuid = $1::uuid", user_uuid)
    if panel_id is not None:
        return int(panel_id)
    try:
        from shared.data_access import resolve_panel_user_id as panel_resolve
        resolved = await panel_resolve(user_uuid)
        return int(resolved) if resolved is not None else None
    except Exception:
        return None


async def execute(
    db, *, action_id: str, user_uuid: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """Выполнить действие. Всегда возвращает результат, не бросает —
    аудит-запись нужна и для неудачных попыток."""
    from web.backend.core.plugin_api import panel_api

    if action_id not in ACTION_IDS:
        return {"ok": False, "message": f"Неизвестное действие: {action_id}"}

    panel_user_id = await resolve_panel_user_id(db, user_uuid)
    if panel_user_id is None:
        return {"ok": False, "message": "Пользователь не найден в панели"}

    api = panel_api()
    try:
        if action_id == "reset_traffic":
            await api.reset_user_traffic(panel_user_id)
            return {"ok": True, "message": "Трафик сброшен"}

        if action_id == "extend_subscription":
            days = _int_param(params.get("days"), default=30, low=1, high=365)
            await api.extend_user_expiration(panel_user_id, days)
            return {"ok": True, "message": f"Подписка продлена на {days} дн."}

        if action_id == "enable_user":
            await api.enable_user(panel_user_id)
            return {"ok": True, "message": "Пользователь разблокирован"}

        if action_id == "disable_user":
            await api.disable_user(panel_user_id)
            return {"ok": True, "message": "Пользователь заблокирован"}

        if action_id == "revoke_subscription":
            only_passwords = bool(params.get("revoke_only_passwords", True))
            await api.revoke_user_subscription(
                panel_user_id, revoke_only_passwords=only_passwords
            )
            message = (
                "Пароли подключения перевыпущены, ссылка подписки прежняя"
                if only_passwords else
                "Подписка перевыпущена — клиенту нужна новая ссылка"
            )
            return {"ok": True, "message": message}

    except Exception as exc:  # noqa: BLE001 — текст ошибки панели нужен саппорту
        return {"ok": False, "message": f"Панель отклонила действие: {exc}"}

    return {"ok": False, "message": f"Действие {action_id} не реализовано"}


def _int_param(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))
