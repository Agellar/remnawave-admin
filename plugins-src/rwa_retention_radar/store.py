"""Своя таблица плагина: ежедневный срез размеров сегментов.

У панели такой истории нет, а без неё дашборд отвечает только на вопрос
«сколько сейчас» и не отвечает на «стало хуже или лучше». Пишем по одной
строке на день и сегмент, перезаписывая сегодняшнюю при каждом пересчёте.

Плагин не может ехать в alembic панели, поэтому схему создаёт сам —
``CREATE TABLE IF NOT EXISTS`` идемпотентен.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List

DDL = """
CREATE TABLE IF NOT EXISTS retention_radar_daily (
    day          DATE NOT NULL,
    segment      TEXT NOT NULL,
    users_count  INTEGER NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (day, segment)
);

CREATE INDEX IF NOT EXISTS retention_radar_daily_day_idx
    ON retention_radar_daily (day DESC);

CREATE TABLE IF NOT EXISTS retention_radar_campaigns (
    id               BIGSERIAL PRIMARY KEY,
    segment          TEXT NOT NULL,
    discount_percent INTEGER NOT NULL,
    valid_hours      INTEGER NOT NULL,
    message_text     TEXT NOT NULL,
    recipients       INTEGER NOT NULL,
    sent             INTEGER NOT NULL DEFAULT 0,
    failed           INTEGER NOT NULL DEFAULT 0,
    dry_run          BOOLEAN NOT NULL DEFAULT TRUE,
    status           TEXT NOT NULL DEFAULT 'open',
    broadcast_id     INTEGER,
    admin_username   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS retention_radar_campaigns_recent_idx
    ON retention_radar_campaigns (created_at DESC);

ALTER TABLE retention_radar_campaigns
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS retention_radar_campaigns_idempotency_idx
    ON retention_radar_campaigns (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS retention_radar_campaign_arms (
    token_hash      TEXT PRIMARY KEY,
    confirm_token   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    admin_account_id BIGINT,
    admin_username  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS retention_radar_campaign_arms_expires_idx
    ON retention_radar_campaign_arms (expires_at);

-- Backward-safe upgrade for installations that created the arms table before
-- account-backed ownership was introduced.  Old, short-lived rows continue to
-- work through the username fallback in consume_arm; all new account-backed
-- rows bind to the immutable admin_accounts.id.
ALTER TABLE retention_radar_campaign_arms
    ADD COLUMN IF NOT EXISTS admin_account_id BIGINT;

-- Кому и когда уже писали. Нужна не для отчётности, а чтобы человек не
-- получил три «персональных предложения» за неделю: перед каждой
-- кампанией список сверяется с этой таблицей.
CREATE TABLE IF NOT EXISTS retention_radar_sends (
    id          BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES retention_radar_campaigns(id) ON DELETE CASCADE,
    user_uuid   UUID NOT NULL,
    telegram_id BIGINT,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS retention_radar_sends_user_idx
    ON retention_radar_sends (user_uuid, sent_at DESC);
"""


async def ensure_schema(db) -> None:
    for statement in (s.strip() for s in DDL.split(";")):
        if statement:
            await db.execute(statement)


async def save_snapshot(db, counts: Dict[str, int]) -> None:
    """Срез за сегодня. Повторный вызов в тот же день перезаписывает
    строку: интересует состояние на конец дня, а не каждый тик."""
    for segment, value in counts.items():
        await db.execute(
            """INSERT INTO retention_radar_daily (day, segment, users_count)
               VALUES (CURRENT_DATE, $1, $2)
               ON CONFLICT (day, segment)
               DO UPDATE SET users_count = EXCLUDED.users_count,
                             computed_at = NOW()""",
            segment,
            int(value),
        )


async def trend(db, segment: str, days: int) -> List[Dict[str, object]]:
    rows = await db.fetch(
        """SELECT day, users_count FROM retention_radar_daily
           WHERE segment = $1 AND day > CURRENT_DATE - $2::int
           ORDER BY day""",
        segment,
        int(days),
    )
    return [{"day": r["day"], "users_count": r["users_count"]} for r in rows]


async def previous_value(db, segment: str) -> int | None:
    """Значение за вчера — для дельты на плитке. Если плагин поставили
    сегодня, истории ещё нет и дельту показывать нечестно."""
    value = await db.fetchval(
        """SELECT users_count FROM retention_radar_daily
           WHERE segment = $1 AND day = CURRENT_DATE - 1""",
        segment,
    )
    return int(value) if value is not None else None


async def first_day(db) -> date | None:
    return await db.fetchval("SELECT min(day) FROM retention_radar_daily")


# ── кампании ─────────────────────────────────────────────────────

async def recently_messaged(db, cooldown_days: int) -> set:
    """UUID тех, кому писали за последние N дней. Пробные прогоны
    (dry_run) не считаются — там никто ничего не получал."""
    rows = await db.fetch(
        """SELECT DISTINCT s.user_uuid
             FROM retention_radar_sends s
             JOIN retention_radar_campaigns c ON c.id = s.campaign_id
            WHERE s.sent_at > NOW() - ($1::int * INTERVAL '1 day')
              AND c.dry_run = FALSE""",
        int(cooldown_days),
    )
    return {str(r["user_uuid"]) for r in rows}


async def open_campaign(
    db,
    *,
    segment: str,
    discount_percent: int,
    valid_hours: int,
    message_text: str,
    recipients: int,
    dry_run: bool,
    admin_username: str | None,
    idempotency_key: str | None = None,
) -> tuple[int, bool]:
    """Запись создаётся ДО отправки: если процесс упадёт на середине,
    в истории останется след, а не тишина."""
    campaign_id = await db.fetchval(
        """INSERT INTO retention_radar_campaigns
               (segment, discount_percent, valid_hours, message_text,
                recipients, dry_run, admin_username, idempotency_key)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
           DO NOTHING
           RETURNING id""",
        segment,
        int(discount_percent),
        int(valid_hours),
        message_text,
        int(recipients),
        bool(dry_run),
        admin_username,
        idempotency_key,
    )
    if campaign_id:
        return int(campaign_id), True
    existing = await db.fetchval(
        "SELECT id FROM retention_radar_campaigns WHERE idempotency_key=$1",
        idempotency_key,
    )
    return int(existing), False


async def campaign_by_idempotency(db, key: str | None):
    if not key:
        return None
    row = await db.fetchrow(
        """SELECT id, recipients, sent, failed, dry_run, status, broadcast_id
             FROM retention_radar_campaigns WHERE idempotency_key=$1""",
        key,
    )
    return dict(row) if row else None


async def issue_arm(
    db,
    *,
    token_hash: str,
    confirm_token: str,
    idempotency_key: str,
    admin_account_id: int | None,
    admin_username: str | None,
    ttl_minutes: int,
) -> None:
    await db.execute(
        """DELETE FROM retention_radar_campaign_arms
            WHERE expires_at < NOW() - INTERVAL '1 day'"""
    )
    await db.execute(
        """INSERT INTO retention_radar_campaign_arms
                  (token_hash, confirm_token, idempotency_key,
                   admin_account_id, admin_username, expires_at)
            VALUES ($1,$2,$3,$4,$5,NOW()+make_interval(mins => $6))""",
        token_hash,
        confirm_token,
        idempotency_key,
        admin_account_id,
        admin_username,
        int(ttl_minutes),
    )


async def consume_arm(
    db,
    *,
    token_hash: str,
    confirm_token: str,
    idempotency_key: str,
    admin_account_id: int | None,
    admin_username: str | None,
) -> bool:
    arm_id = await db.fetchval(
        """UPDATE retention_radar_campaign_arms
              SET used_at=NOW()
            WHERE token_hash=$1 AND confirm_token=$2 AND idempotency_key=$3
              AND (
                    (admin_account_id IS NOT NULL AND admin_account_id=$4)
                    OR
                    (admin_account_id IS NULL
                     AND admin_username IS NOT DISTINCT FROM $5)
                  )
              AND used_at IS NULL AND expires_at > NOW()
            RETURNING token_hash""",
        token_hash,
        confirm_token,
        idempotency_key,
        admin_account_id,
        admin_username,
    )
    return bool(arm_id)


async def close_campaign(
    db, campaign_id: int, *, status: str, sent: int, failed: int, broadcast_id: int | None
) -> None:
    await db.execute(
        """UPDATE retention_radar_campaigns
              SET status = $2, sent = $3, failed = $4,
                  broadcast_id = $5, finished_at = NOW()
            WHERE id = $1""",
        int(campaign_id),
        status,
        int(sent),
        int(failed),
        broadcast_id,
    )


async def record_sends(db, campaign_id: int, people: list) -> None:
    for user in people:
        await db.execute(
            """INSERT INTO retention_radar_sends (campaign_id, user_uuid, telegram_id)
               VALUES ($1, $2::uuid, $3)""",
            int(campaign_id),
            user["uuid"],
            user.get("telegram_id"),
        )


async def list_campaigns(db, limit: int = 20) -> list:
    rows = await db.fetch(
        """SELECT id, segment, discount_percent, valid_hours, message_text,
                  recipients, sent, failed, dry_run, status, broadcast_id,
                  admin_username, created_at, finished_at
             FROM retention_radar_campaigns
            ORDER BY created_at DESC
            LIMIT $1""",
        int(limit),
    )
    return [dict(r) for r in rows]
