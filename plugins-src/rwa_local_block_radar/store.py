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
    node_alive BOOLEAN NOT NULL,
    feedback TEXT,
    feedback_at TIMESTAMPTZ
);
ALTER TABLE local_block_radar_alerts
    ADD COLUMN IF NOT EXISTS feedback TEXT;
ALTER TABLE local_block_radar_alerts
    ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS local_block_radar_one_open_alert_idx
    ON local_block_radar_alerts (node_uuid) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS local_block_radar_alerts_since_idx
    ON local_block_radar_alerts (since DESC);

CREATE TABLE IF NOT EXISTS local_block_radar_ai_analyses (
    id BIGSERIAL PRIMARY KEY,
    alert_id BIGINT NOT NULL REFERENCES local_block_radar_alerts(id) ON DELETE CASCADE,
    classification TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    summary TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
    support_note TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'available',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (alert_id)
);
ALTER TABLE local_block_radar_ai_analyses
    ADD COLUMN IF NOT EXISTS availability TEXT NOT NULL DEFAULT 'available';
CREATE INDEX IF NOT EXISTS local_block_radar_ai_updated_idx
    ON local_block_radar_ai_analyses (updated_at DESC);

CREATE TABLE IF NOT EXISTS local_block_radar_ai_usage (
    period TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0,
    attempted INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE local_block_radar_ai_usage
    ADD COLUMN IF NOT EXISTS attempted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE local_block_radar_ai_usage
    ADD COLUMN IF NOT EXISTS succeeded INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS local_block_radar_probe_cycles (
    id BIGSERIAL PRIMARY KEY,
    target_uuid UUID NOT NULL,
    target_name TEXT NOT NULL,
    target_port INTEGER NOT NULL,
    state TEXT NOT NULL,
    ru_success INTEGER NOT NULL DEFAULT 0,
    ru_total INTEGER NOT NULL DEFAULT 0,
    control_success INTEGER NOT NULL DEFAULT 0,
    control_total INTEGER NOT NULL DEFAULT 0,
    node_success INTEGER NOT NULL DEFAULT 0,
    node_total INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    incident_open BOOLEAN NOT NULL DEFAULT FALSE,
    globalping_measurement_id TEXT,
    error_code TEXT,
    sampled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS local_block_radar_probe_cycles_target_at_idx
    ON local_block_radar_probe_cycles (target_uuid, sampled_at DESC);
CREATE INDEX IF NOT EXISTS local_block_radar_probe_cycles_at_idx
    ON local_block_radar_probe_cycles (sampled_at DESC);

CREATE TABLE IF NOT EXISTS local_block_radar_probe_results (
    id BIGSERIAL PRIMARY KEY,
    cycle_id BIGINT NOT NULL REFERENCES local_block_radar_probe_cycles(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    vantage_label TEXT NOT NULL,
    country TEXT,
    asn INTEGER,
    network TEXT,
    success BOOLEAN NOT NULL,
    latency_ms DOUBLE PRECISION,
    error_code TEXT,
    sampled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS local_block_radar_probe_results_cycle_idx
    ON local_block_radar_probe_results (cycle_id);
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
    await db.execute(
        "DELETE FROM local_block_radar_probe_cycles WHERE sampled_at < NOW() - make_interval(days => $1)",
        days,
    )


async def previous_probe_cycle(db, target_uuid: str) -> dict | None:
    row = await db.fetchrow(
        """SELECT * FROM local_block_radar_probe_cycles
           WHERE target_uuid=$1::uuid ORDER BY sampled_at DESC LIMIT 1""",
        target_uuid,
    )
    return dict(row) if row else None


async def save_probe_cycle(
    db, *, target: dict, summary: dict, results: list[dict]
) -> int:
    async with db.acquire() as conn:
        async with conn.transaction():
            cycle_id = int(await conn.fetchval(
                """INSERT INTO local_block_radar_probe_cycles
                          (target_uuid,target_name,target_port,state,ru_success,ru_total,
                           control_success,control_total,node_success,node_total,
                           consecutive_failures,incident_open,globalping_measurement_id,error_code)
                   VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                   RETURNING id""",
                target["uuid"], target["name"], int(target["port"]), summary["state"],
                summary["ru_success"], summary["ru_total"], summary["control_success"],
                summary["control_total"], summary["node_success"], summary["node_total"],
                summary["consecutive_failures"], summary["incident_open"],
                summary.get("measurement_id"), summary.get("error_code"),
            ))
            if results:
                await conn.executemany(
                    """INSERT INTO local_block_radar_probe_results
                              (cycle_id,source,vantage_label,country,asn,network,
                               success,latency_ms,error_code)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    [(
                        cycle_id, row["source"], row["vantage_label"], row.get("country"),
                        row.get("asn"), row.get("network"), bool(row["success"]),
                        row.get("latency_ms"), row.get("error_code"),
                    ) for row in results],
                )
    return cycle_id


async def probe_overview(db) -> list[dict]:
    rows = await db.fetch(
        """SELECT c.*,
                  COALESCE(jsonb_agg(jsonb_build_object(
                    'source',r.source,'vantage_label',r.vantage_label,'country',r.country,
                    'asn',r.asn,'network',r.network,'success',r.success,
                    'latency_ms',r.latency_ms,'error_code',r.error_code
                  ) ORDER BY r.source,r.vantage_label)
                  FILTER (WHERE r.id IS NOT NULL),'[]'::jsonb) AS results
             FROM local_block_radar_probe_cycles c
        LEFT JOIN local_block_radar_probe_results r ON r.cycle_id=c.id
            WHERE c.id IN (
              SELECT DISTINCT ON (target_uuid) id
                FROM local_block_radar_probe_cycles
               ORDER BY target_uuid, sampled_at DESC
            )
         GROUP BY c.id ORDER BY c.target_name"""
    )
    return [dict(row) for row in rows]


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


async def alert_by_id(db, alert_id: int):
    row = await db.fetchrow(
        "SELECT * FROM local_block_radar_alerts WHERE id=$1", int(alert_id)
    )
    return dict(row) if row else None


async def set_feedback(db, alert_id: int, verdict: str) -> dict | None:
    row = await db.fetchrow(
        """UPDATE local_block_radar_alerts
              SET feedback=$2, feedback_at=NOW()
            WHERE id=$1
        RETURNING id, feedback, feedback_at""",
        int(alert_id), verdict,
    )
    return dict(row) if row else None


async def analysis_for_alert(db, alert_id: int) -> dict | None:
    row = await db.fetchrow(
        "SELECT * FROM local_block_radar_ai_analyses WHERE alert_id=$1", int(alert_id)
    )
    return dict(row) if row else None


async def analysis_context(db, alert: dict, *, sample_limit: int = 12) -> dict:
    from shared.agent_version import LATEST_AGENT_VERSION

    samples = await db.fetch(
        """SELECT online, total_online, share, node_alive, agent_version, sampled_at
             FROM local_block_radar_samples
            WHERE node_uuid=$1::uuid
            ORDER BY sampled_at DESC LIMIT $2""",
        str(alert["node_uuid"]), int(sample_limit),
    )
    latest_nodes = await db.fetch(
        """SELECT DISTINCT ON (node_uuid)
                  node_name, provider_name, transport, online, total_online,
                  share, node_alive, agent_version, sampled_at
             FROM local_block_radar_samples
            ORDER BY node_uuid, sampled_at DESC"""
    )
    return {
        "reference_agent_version": LATEST_AGENT_VERSION,
        "alert": {
            "id": int(alert["id"]),
            "node_name": alert["node_name"],
            "provider_name": alert["provider_name"],
            "transport": alert["transport"],
            "since": alert["since"],
            "resolved_at": alert["resolved_at"],
            "online": int(alert["online"]),
            "baseline_online": round(float(alert["baseline_online"]), 3),
            "share": round(float(alert["share"]), 6),
            "baseline_share": round(float(alert["baseline_share"]), 6),
            "node_alive": bool(alert["node_alive"]),
        },
        "recent_samples_newest_first": [
            {
                "online": int(row["online"]),
                "total_online": int(row["total_online"]),
                "share": round(float(row["share"]), 6),
                "node_alive": bool(row["node_alive"]),
                "agent_version": row["agent_version"] or "unknown",
                "sampled_at": row["sampled_at"],
            }
            for row in samples
        ],
        "network_latest": [
            {
                "node_name": row["node_name"],
                "provider_name": row["provider_name"],
                "transport": row["transport"],
                "online": int(row["online"]),
                "total_online": int(row["total_online"]),
                "share": round(float(row["share"]), 6),
                "node_alive": bool(row["node_alive"]),
                "agent_version": row["agent_version"] or "unknown",
                "sampled_at": row["sampled_at"],
            }
            for row in latest_nodes
        ],
    }


async def reserve_ai_call(db, *, period: str, limit: int) -> int | None:
    value = await db.fetchval(
        """INSERT INTO local_block_radar_ai_usage
                   (period, used, attempted, succeeded) VALUES ($1, 1, 1, 0)
           ON CONFLICT (period) DO UPDATE
             SET used=GREATEST(local_block_radar_ai_usage.used,
                               local_block_radar_ai_usage.attempted)+1,
                 attempted=GREATEST(local_block_radar_ai_usage.used,
                                    local_block_radar_ai_usage.attempted)+1
           WHERE GREATEST(local_block_radar_ai_usage.used,
                          local_block_radar_ai_usage.attempted) < $2
           RETURNING attempted""",
        period, int(limit),
    )
    return int(value) if value is not None else None


async def ai_usage(db, period: str) -> int:
    return int(
        await db.fetchval(
            "SELECT used FROM local_block_radar_ai_usage WHERE period=$1", period
        )
        or 0
    )


async def ai_usage_counts(db, period: str) -> dict[str, int]:
    row = await db.fetchrow(
        """SELECT GREATEST(used, attempted)::int AS attempted,
                  succeeded::int AS succeeded
             FROM local_block_radar_ai_usage WHERE period=$1""",
        period,
    )
    return {
        "attempted": int(row["attempted"] or 0) if row else 0,
        "succeeded": int(row["succeeded"] or 0) if row else 0,
    }


async def record_ai_success(db, *, period: str) -> None:
    await db.execute(
        """UPDATE local_block_radar_ai_usage
              SET succeeded=succeeded+1
            WHERE period=$1""",
        period,
    )


async def save_analysis(
    db, *, alert_id: int, result: dict, provider: str, model: str, input_hash: str
) -> dict:
    import json

    row = await db.fetchrow(
        """INSERT INTO local_block_radar_ai_analyses
                   (alert_id, classification, confidence, summary, evidence,
                    recommendations, support_note, provider, model, input_hash,
                    availability)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11)
           ON CONFLICT (alert_id) DO UPDATE SET
               classification=EXCLUDED.classification,
               confidence=EXCLUDED.confidence,
               summary=EXCLUDED.summary,
               evidence=EXCLUDED.evidence,
               recommendations=EXCLUDED.recommendations,
               support_note=EXCLUDED.support_note,
               provider=EXCLUDED.provider,
               model=EXCLUDED.model,
               input_hash=EXCLUDED.input_hash,
               availability=EXCLUDED.availability,
               updated_at=NOW()
           RETURNING *""",
        int(alert_id), result["classification"], float(result["confidence"]),
        result["summary"], json.dumps(result["evidence"], ensure_ascii=False),
        json.dumps(result["recommendations"], ensure_ascii=False),
        result.get("support_note"), provider, model, input_hash,
        result.get("availability", "available"),
    )
    return dict(row)
