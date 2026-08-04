"""Сегменты оттока: SQL поверх локального кэша панели.

Источник «когда человек последний раз был в сети» — поле ``onlineAt``
из ``raw_data`` (панель обновляет его синком; заполнено почти у всех).
История ``user_connections`` используется только там, где нужен факт
«не подключался ни разу вообще» — там важно отсутствие записей, а не дата.

Все запросы идут от одного CTE ``base``, чтобы форма строки была
одинаковой во всех сегментах: фронт рисует их одной таблицей.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

# Ключи сегментов — они же в URL, в i18n и в таблице срезов.
SEGMENTS = ("silent", "expiring", "lapsed", "stalled")

_BASE_CTE = """
WITH base AS (
    SELECT u.uuid,
           u.username,
           u.telegram_id,
           u.status,
           u.expire_at,
           u.created_at,
           u.used_traffic_bytes,
           u.traffic_limit_bytes,
           u.tag,
           NULLIF(u.raw_data -> 'userTraffic' ->> 'onlineAt', '')::timestamptz AS last_online
    FROM users u
)
"""

_ROW_COLUMNS = """
    uuid, username, telegram_id, status, expire_at, created_at,
    used_traffic_bytes, traffic_limit_bytes, tag, last_online
"""


def _segment_where(key: str, th: Dict[str, float]) -> Tuple[str, List[Any]]:
    """Условие сегмента и его параметры. Возвращаем именно пару, чтобы
    один и тот же фильтр использовали и счётчик, и выборка списка."""
    if key == "silent":
        # Подписка ещё действует, а человек уже не приходит. Требуем
        # непустой last_online: тот, кто не заходил ни разу, — это не
        # «ушёл», это «не завёлся», отдельный сегмент.
        return (
            """status = 'ACTIVE'
               AND (expire_at IS NULL OR expire_at > NOW())
               AND last_online IS NOT NULL
               AND last_online < NOW() - ($1::int * INTERVAL '1 day')""",
            [int(th["silent_days"])],
        )
    if key == "expiring":
        return (
            """status = 'ACTIVE'
               AND expire_at IS NOT NULL
               AND expire_at > NOW()
               AND expire_at <= NOW() + ($1::int * INTERVAL '1 day')""",
            [int(th["expiring_days"])],
        )
    if key == "lapsed":
        return (
            """status = 'EXPIRED'
               AND expire_at IS NOT NULL
               AND expire_at <= NOW()
               AND expire_at >= NOW() - ($1::int * INTERVAL '1 day')""",
            [int(th["lapsed_days"])],
        )
    if key == "stalled":
        # Завели недавно и ни одного подключения — деньги уже взяли,
        # а человек до сервиса так и не дошёл.
        return (
            """created_at >= NOW() - ($1::int * INTERVAL '1 day')
               AND last_online IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM user_connections c WHERE c.user_uuid = base.uuid
               )""",
            [int(th["onboarding_days"])],
        )
    raise ValueError(f"unknown_segment:{key}")


_ORDER = {
    # Дольше всех молчащие — сверху.
    "silent": "last_online ASC",
    # Ближайшее истечение — сверху.
    "expiring": "expire_at ASC",
    # Недавно истёкшие возвращаются охотнее давних.
    "lapsed": "expire_at DESC",
    "stalled": "created_at DESC",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(value: datetime | None) -> int | None:
    return (_now() - value).days if value else None


def _row(record, th: Dict[str, float], segment: str) -> Dict[str, Any]:
    last_online = record["last_online"]
    days_silent = _days_since(last_online)

    # «В зоне риска» значит разное по сегментам, поэтому считаем здесь,
    # а не в SQL: у истекающих это тишина перед продлением, у ушедших —
    # глубина молчания.
    if segment == "expiring":
        at_risk = days_silent is None or days_silent >= int(th["expiring_risk_days"])
    elif segment == "silent":
        at_risk = days_silent is not None and days_silent >= int(th["silent_deep_days"])
    else:
        at_risk = False

    return {
        "uuid": str(record["uuid"]),
        "username": record["username"],
        "telegram_id": record["telegram_id"],
        "status": record["status"],
        "expire_at": record["expire_at"],
        "created_at": record["created_at"],
        "last_online": last_online,
        "days_silent": days_silent,
        "days_until_expire": (
            (record["expire_at"] - _now()).days if record["expire_at"] else None
        ),
        "used_traffic_bytes": record["used_traffic_bytes"],
        "traffic_limit_bytes": record["traffic_limit_bytes"],
        "tag": record["tag"],
        "at_risk": at_risk,
    }


async def count_segment(db, key: str, th: Dict[str, float]) -> int:
    where, params = _segment_where(key, th)
    value = await db.fetchval(
        f"{_BASE_CTE} SELECT count(*) FROM base WHERE {where}", *params
    )
    return int(value or 0)


async def list_segment(
    db, key: str, th: Dict[str, float], limit: int, offset: int
) -> List[Dict[str, Any]]:
    where, params = _segment_where(key, th)
    rows = await db.fetch(
        f"""{_BASE_CTE}
            SELECT {_ROW_COLUMNS} FROM base
             WHERE {where}
             ORDER BY {_ORDER[key]}
             LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
        *params,
        limit,
        offset,
    )
    return [_row(r, th, key) for r in rows]


async def counts(db, th: Dict[str, float]) -> Dict[str, int]:
    return {key: await count_segment(db, key, th) for key in SEGMENTS}


async def totals(db) -> Dict[str, int]:
    row = await db.fetchrow(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE status = 'ACTIVE') AS active
             FROM users"""
    )
    return {"total": int(row["total"]), "active": int(row["active"])}


async def attention(db, th: Dict[str, float], limit: int = 10) -> List[Dict[str, Any]]:
    """«Горит прямо сейчас»: подписка кончается на днях, а человек уже
    молчит. Самый дешёвый способ вернуть деньги — написать этим первыми."""
    rows = await db.fetch(
        f"""{_BASE_CTE}
            SELECT {_ROW_COLUMNS} FROM base
             WHERE status = 'ACTIVE'
               AND expire_at IS NOT NULL
               AND expire_at > NOW()
               AND expire_at <= NOW() + ($1::int * INTERVAL '1 day')
               AND (last_online IS NULL
                    OR last_online < NOW() - ($2::int * INTERVAL '1 day'))
             ORDER BY expire_at ASC
             LIMIT $3""",
        int(th["expiring_days"]),
        int(th["expiring_risk_days"]),
        int(limit),
    )
    return [_row(r, th, "expiring") for r in rows]
