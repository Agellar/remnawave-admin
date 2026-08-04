"""Собственные таблицы плагина.

Плагин не может ехать в alembic панели, поэтому схему создаёт сам при
старте: три маленькие таблицы под аудит действий, кэш корреляций и счётчик
ИИ-вызовов. ``CREATE TABLE IF NOT EXISTS`` — операция идемпотентная, так
что повторный старт ничего не ломает.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DDL = """
CREATE TABLE IF NOT EXISTS smart_support_sessions (
    id                   BIGSERIAL PRIMARY KEY,
    opened_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    admin_username       TEXT,
    target_user_uuid     UUID,
    triggered_by_rule_id TEXT,
    action_id            TEXT,
    ok                   BOOLEAN,
    message              TEXT,
    params               JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS smart_support_sessions_user_idx
    ON smart_support_sessions (target_user_uuid, opened_at DESC);
CREATE INDEX IF NOT EXISTS smart_support_sessions_recent_idx
    ON smart_support_sessions (opened_at DESC);

CREATE TABLE IF NOT EXISTS smart_support_correlations (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL,
    key            TEXT NOT NULL,
    label          TEXT,
    affected_users INTEGER NOT NULL,
    member_uuids   TEXT[] NOT NULL DEFAULT '{}',
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS smart_support_correlations_computed_idx
    ON smart_support_correlations (computed_at DESC);

CREATE TABLE IF NOT EXISTS smart_support_ai_usage (
    period TEXT PRIMARY KEY,
    used   INTEGER NOT NULL DEFAULT 0
);
"""


async def ensure_schema(db) -> None:
    for statement in (s.strip() for s in DDL.split(";")):
        if statement:
            await db.execute(statement)


# ── журнал действий ──────────────────────────────────────────────

async def log_action(
    db,
    *,
    admin_username: Optional[str],
    target_user_uuid: Optional[str],
    action_id: Optional[str],
    triggered_by_rule_id: Optional[str],
    ok: Optional[bool],
    message: Optional[str],
    params: Dict[str, Any],
) -> Optional[int]:
    """Append-only запись. Падение аудита не должно ронять само действие,
    поэтому ошибку глотаем — вызывающий уже сделал работу."""
    try:
        return await db.fetchval(
            """INSERT INTO smart_support_sessions
                   (admin_username, target_user_uuid, triggered_by_rule_id,
                    action_id, ok, message, params)
               VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb)
               RETURNING id""",
            admin_username,
            target_user_uuid,
            triggered_by_rule_id,
            action_id,
            ok,
            message,
            json.dumps(params or {}, ensure_ascii=False, default=str),
        )
    except Exception:
        return None


def _row_to_session(row) -> Dict[str, Any]:
    params = row["params"]
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    return {
        "id": row["id"],
        "opened_at": row["opened_at"],
        "admin_username": row["admin_username"],
        "target_user_uuid": str(row["target_user_uuid"]) if row["target_user_uuid"] else None,
        "triggered_by_rule_id": row["triggered_by_rule_id"],
        "action_id": row["action_id"],
        "ok": row["ok"],
        "message": row["message"],
        "params": params or {},
    }


async def sessions_for_user(db, user_uuid: str, limit: int, offset: int):
    rows = await db.fetch(
        """SELECT * FROM smart_support_sessions
           WHERE target_user_uuid = $1::uuid
           ORDER BY opened_at DESC LIMIT $2 OFFSET $3""",
        user_uuid, limit, offset,
    )
    total = await db.fetchval(
        "SELECT COUNT(*) FROM smart_support_sessions WHERE target_user_uuid = $1::uuid",
        user_uuid,
    )
    return [_row_to_session(r) for r in rows], int(total or 0)


async def sessions_recent(
    db,
    *,
    limit: int,
    offset: int,
    action_id: Optional[str] = None,
    admin_username: Optional[str] = None,
):
    """Журнал с необязательными фильтрами. Условия собираем позиционно —
    asyncpg не умеет именованные параметры."""
    where: List[str] = []
    args: List[Any] = []
    if action_id:
        args.append(action_id)
        where.append(f"action_id = ${len(args)}")
    if admin_username:
        args.append(f"%{admin_username}%")
        where.append(f"admin_username ILIKE ${len(args)}")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM smart_support_sessions {clause}", *args
    )
    rows = await db.fetch(
        f"""SELECT * FROM smart_support_sessions {clause}
            ORDER BY opened_at DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
        *args, limit, offset,
    )
    return [_row_to_session(r) for r in rows], int(total or 0)


# ── корреляции ───────────────────────────────────────────────────

async def replace_correlations(db, clusters: List[Dict[str, Any]], max_age_minutes: float) -> None:
    """Записать свежий срез и подчистить протухший."""
    await db.execute(
        "DELETE FROM smart_support_correlations "
        "WHERE computed_at < NOW() - ($1 || ' minutes')::interval",
        str(int(max_age_minutes)),
    )
    for c in clusters:
        await db.execute(
            """INSERT INTO smart_support_correlations
                   (kind, key, label, affected_users, member_uuids, window_start, window_end)
               VALUES ($1, $2, $3, $4, $5::text[], $6, $7)""",
            c["kind"], c["key"], c.get("label"), int(c["affected_users"]),
            [str(u) for u in c.get("member_uuids", [])],
            c["window_start"], c["window_end"],
        )


async def correlations_for_user(db, user_uuid: str, max_age_minutes: float):
    rows = await db.fetch(
        """SELECT kind, key, label, affected_users, window_start, window_end
           FROM smart_support_correlations
           WHERE $1 = ANY(member_uuids)
             AND computed_at >= NOW() - ($2 || ' minutes')::interval
           ORDER BY affected_users DESC""",
        str(user_uuid), str(int(max_age_minutes)),
    )
    return [dict(r) for r in rows]


# ── счётчик ИИ-вызовов ───────────────────────────────────────────

def current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def ai_usage(db) -> int:
    used = await db.fetchval(
        "SELECT used FROM smart_support_ai_usage WHERE period = $1", current_period()
    )
    return int(used or 0)


async def bump_ai_usage(db) -> int:
    return int(await db.fetchval(
        """INSERT INTO smart_support_ai_usage (period, used) VALUES ($1, 1)
           ON CONFLICT (period) DO UPDATE SET used = smart_support_ai_usage.used + 1
           RETURNING used""",
        current_period(),
    ) or 0)
