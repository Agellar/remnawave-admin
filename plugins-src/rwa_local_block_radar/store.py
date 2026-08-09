"""Persistence for local radar samples and incidents."""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS local_block_radar_samples (
    id BIGSERIAL PRIMARY KEY,
    node_uuid UUID NOT NULL,
    node_name TEXT NOT NULL,
    provider_name TEXT,
    transport TEXT NOT NULL DEFAULT 'mixed',
    online INTEGER NOT NULL,
    total_online INTEGER NOT NULL,
    share DOUBLE PRECISION NOT NULL,
    node_alive BOOLEAN NOT NULL,
    agent_version TEXT,
    sampled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS local_block_radar_samples_node_at_idx
    ON local_block_radar_samples (node_uuid, sampled_at DESC);
CREATE INDEX IF NOT EXISTS local_block_radar_samples_at_idx
    ON local_block_radar_samples (sampled_at DESC);

CREATE TABLE IF NOT EXISTS local_block_radar_alerts (
    id BIGSERIAL PRIMARY KEY,
    node_uuid UUID NOT NULL,
    node_name TEXT NOT NULL,
    provider_name TEXT,
    transport TEXT NOT NULL DEFAULT 'mixed',
    since TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    online INTEGER NOT NULL,
    baseline_online DOUBLE PRECISION NOT NULL,
    share DOUBLE PRECISION NOT NULL,
    baseline_share DOUBLE PRECISION NOT NULL,
    node_alive BOOLEAN NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS local_block_radar_one_open_alert_idx
    ON local_block_radar_alerts (node_uuid) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS local_block_radar_alerts_since_idx
    ON local_block_radar_alerts (since DESC);
"""


async def ensure_schema(db) -> None:
    for statement in (item.strip() for item in DDL.split(";")):
        if statement:
            await db.execute(statement)


async def insert_samples(db, rows: list[dict]) -> None:
    if not rows:
        return
    async with db.acquire() as conn:
        await conn.executemany(
            """INSERT INTO local_block_radar_samples
                   (node_uuid, node_name, provider_name, transport, online,
                    total_online, share, node_alive, agent_version)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)""",
            [
                (
                    row["node_uuid"], row["node_name"], row["provider_name"],
                    row["transport"], row["online"], row["total_online"],
                    row["share"], row["node_alive"], row["agent_version"],
                )
                for row in rows
            ],
        )


async def cleanup(db, days: int) -> None:
    await db.execute(
        "DELETE FROM local_block_radar_samples WHERE sampled_at < NOW() - make_interval(days => $1)",
        days,
    )


async def baseline(db, node_uuid: str, days: int) -> dict | None:
    row = await db.fetchrow(
        """SELECT COUNT(*)::int AS samples,
                  MIN(sampled_at) AS first_at,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY online) AS online,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY share) AS share
           FROM local_block_radar_samples
           WHERE node_uuid = $1::uuid
             AND sampled_at >= NOW() - make_interval(days => $2)
             AND sampled_at < NOW() - INTERVAL '15 minutes'""",
        node_uuid, days,
    )
    if not row or not row["samples"]:
        return None
    return dict(row)


async def recent_samples(db, node_uuid: str, limit: int) -> list[dict]:
    rows = await db.fetch(
        """SELECT online, share, node_alive, sampled_at
           FROM local_block_radar_samples WHERE node_uuid = $1::uuid
           ORDER BY sampled_at DESC LIMIT $2""",
        node_uuid, limit,
    )
    return [dict(row) for row in rows]


async def open_alert(db, node_uuid: str):
    return await db.fetchrow(
        "SELECT * FROM local_block_radar_alerts WHERE node_uuid=$1::uuid AND resolved_at IS NULL",
        node_uuid,
    )


async def create_alert(db, row: dict, base: dict) -> int:
    return int(await db.fetchval(
        """INSERT INTO local_block_radar_alerts
                   (node_uuid, node_name, provider_name, transport, online,
                    baseline_online, share, baseline_share, node_alive)
               VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (node_uuid) WHERE resolved_at IS NULL DO NOTHING
               RETURNING id""",
        row["node_uuid"], row["node_name"], row["provider_name"], row["transport"],
        row["online"], base["online"], row["share"], base["share"], row["node_alive"],
    ) or 0)


async def resolve_alert(db, alert_id: int) -> None:
    await db.execute(
        "UPDATE local_block_radar_alerts SET resolved_at=NOW() WHERE id=$1 AND resolved_at IS NULL",
        alert_id,
    )
