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
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_users    INTEGER
);

CREATE INDEX IF NOT EXISTS smart_support_correlations_computed_idx
    ON smart_support_correlations (computed_at DESC);

CREATE TABLE IF NOT EXISTS smart_support_ai_usage (
    period TEXT PRIMARY KEY,
    used   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS smart_support_feedback (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT REFERENCES smart_support_sessions(id) ON DELETE SET NULL,
    target_user_uuid UUID NOT NULL,
    rule_id          TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    comment          TEXT,
    admin_username   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS smart_support_feedback_once_idx
    ON smart_support_feedback (session_id, rule_id, admin_username)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS smart_support_feedback_rule_idx
    ON smart_support_feedback (rule_id, created_at DESC);
"""

# Upgrades for installations that already have the pre-1.4.3 table. The
# dedupe runs before the unique index so an old append-only cache cannot make
# startup fail. Every statement is idempotent.
CORRELATION_MIGRATIONS = (
    "ALTER TABLE smart_support_correlations ADD COLUMN IF NOT EXISTS "
    "last_active_at TIMESTAMPTZ",
    "ALTER TABLE smart_support_correlations ADD COLUMN IF NOT EXISTS total_users INTEGER",
    "UPDATE smart_support_correlations SET last_active_at = computed_at "
    "WHERE last_active_at IS NULL",
    "ALTER TABLE smart_support_correlations ALTER COLUMN last_active_at SET DEFAULT NOW()",
    "ALTER TABLE smart_support_correlations ALTER COLUMN last_active_at SET NOT NULL",
    """DELETE FROM smart_support_correlations old
       USING smart_support_correlations fresh
       WHERE old.kind = fresh.kind AND old.key = fresh.key
         AND (old.computed_at < fresh.computed_at OR
              (old.computed_at = fresh.computed_at AND old.id < fresh.id))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS smart_support_correlations_kind_key_idx "
    "ON smart_support_correlations (kind, key)",
)


async def ensure_schema(db) -> None:
    for statement in (s.strip() for s in DDL.split(";")):
        if statement:
            await db.execute(statement)
    for statement in CORRELATION_MIGRATIONS:
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
    visible_user_uuids: Optional[set[str]] = None,
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
    if visible_user_uuids is not None:
        args.append(sorted(visible_user_uuids))
        where.append(f"target_user_uuid = ANY(${len(args)}::uuid[])")
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

async def replace_correlations(
    db, clusters: List[Dict[str, Any]], history_minutes: float
) -> None:
    """Upsert the fresh slice, retaining recently resolved clusters."""
    await db.execute(
        "DELETE FROM smart_support_correlations "
        "WHERE last_active_at < NOW() - ($1 || ' minutes')::interval",
        str(max(1, int(history_minutes))),
    )
    # ASN rows from pre-1.4.3-local builds were derived from violations and can
    # mislabel a large ISP as an outage. They are disposable correlation cache.
    await db.execute("DELETE FROM smart_support_correlations WHERE kind = 'asn'")
    for c in clusters:
        await db.execute(
            """INSERT INTO smart_support_correlations
                   (kind, key, label, affected_users, member_uuids, window_start,
                    window_end, total_users, computed_at, last_active_at)
               VALUES ($1, $2, $3, $4, $5::text[], $6, $7, $8, NOW(), NOW())
               ON CONFLICT (kind, key) DO UPDATE SET
                   label = EXCLUDED.label,
                   affected_users = EXCLUDED.affected_users,
                   member_uuids = EXCLUDED.member_uuids,
                   window_start = EXCLUDED.window_start,
                   window_end = EXCLUDED.window_end,
                   total_users = EXCLUDED.total_users,
                   computed_at = NOW(),
                   last_active_at = NOW()""",
            c["kind"], c["key"], c.get("label"), int(c["affected_users"]),
            [str(u) for u in c.get("member_uuids", [])],
            c["window_start"], c["window_end"], c.get("total_users"),
        )


async def correlations_for_user(
    db,
    user_uuid: str,
    max_age_minutes: float,
    history_minutes: float | None = None,
):
    history = max(int(history_minutes or max_age_minutes), int(max_age_minutes), 1)
    rows = await db.fetch(
        """SELECT kind, key, label, affected_users, total_users,
                  window_start, window_end,
                  (last_active_at >= NOW() - ($2 || ' minutes')::interval) AS is_active,
                  EXTRACT(EPOCH FROM (NOW() - last_active_at)) / 60.0 AS age_minutes
           FROM smart_support_correlations
           WHERE $1 = ANY(member_uuids)
              AND last_active_at >= NOW() - ($3 || ' minutes')::interval
           ORDER BY is_active DESC, affected_users DESC""",
        str(user_uuid), str(max(1, int(max_age_minutes))), str(history),
    )
    result = []
    for row in rows:
        item = dict(row)
        if item.get("age_minutes") is not None:
            item["age_minutes"] = max(0, int(float(item["age_minutes"])))
        result.append(item)
    return result


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


async def reserve_ai_usage(db, limit: int = 0) -> int:
    """Atomically reserve one outbound AI call within the monthly cap.

    ``limit=0`` means unlimited. Returning zero means another request already
    consumed the final slot, so the caller must not contact a provider.
    """
    return int(await db.fetchval(
        """INSERT INTO smart_support_ai_usage (period, used) VALUES ($1, 1)
           ON CONFLICT (period) DO UPDATE
             SET used = smart_support_ai_usage.used + 1
             WHERE $2 = 0 OR smart_support_ai_usage.used < $2
           RETURNING used""",
        current_period(), max(0, int(limit)),
    ) or 0)


async def save_feedback(
    db,
    *,
    session_id: int | None,
    target_user_uuid: str,
    rule_id: str,
    verdict: str,
    comment: str | None,
    admin_username: str | None,
) -> int:
    """Store operator judgement; repeat clicks update the same judgement."""
    if session_id is None:
        return int(
            await db.fetchval(
                """INSERT INTO smart_support_feedback
                           (session_id,target_user_uuid,rule_id,verdict,comment,admin_username)
                     VALUES (NULL,$1::uuid,$2,$3,$4,$5) RETURNING id""",
                target_user_uuid, rule_id, verdict, comment, admin_username,
            )
            or 0
        )
    return int(
        await db.fetchval(
            """INSERT INTO smart_support_feedback
                       (session_id,target_user_uuid,rule_id,verdict,comment,admin_username)
                 VALUES ($1,$2::uuid,$3,$4,$5,$6)
                 ON CONFLICT (session_id,rule_id,admin_username)
                     WHERE session_id IS NOT NULL
                 DO UPDATE SET verdict=EXCLUDED.verdict,
                               comment=EXCLUDED.comment,
                               updated_at=NOW()
                 RETURNING id""",
            int(session_id), target_user_uuid, rule_id, verdict, comment, admin_username,
        )
        or 0
    )


async def feedback_summary(db, rule_id: str) -> Dict[str, int]:
    rows = await db.fetch(
        """SELECT verdict, COUNT(*)::int AS count
             FROM smart_support_feedback WHERE rule_id=$1 GROUP BY verdict""",
        rule_id,
    )
    return {str(row["verdict"]): int(row["count"]) for row in rows}
