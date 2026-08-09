"""Small privacy-safe incident bus backed by the panel database.

Only infrastructure dimensions are stored here. User UUIDs, IP addresses and
Telegram identifiers deliberately do not belong in the shared incident table.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

DDL = """
CREATE TABLE IF NOT EXISTS plugin_incidents (
    id BIGSERIAL PRIMARY KEY,
    source_plugin TEXT NOT NULL,
    incident_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    node_uuid UUID,
    transport TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS plugin_incidents_one_active_idx
    ON plugin_incidents (source_plugin, incident_key)
    WHERE status IN ('open', 'acknowledged');
CREATE INDEX IF NOT EXISTS plugin_incidents_active_node_idx
    ON plugin_incidents (node_uuid, updated_at DESC)
    WHERE status IN ('open', 'acknowledged');
CREATE INDEX IF NOT EXISTS plugin_incidents_started_idx
    ON plugin_incidents (started_at DESC);
"""


async def ensure_schema(db) -> None:
    for statement in (item.strip() for item in DDL.split(";")):
        if statement:
            await db.execute(statement)


async def upsert(
    db,
    *,
    source_plugin: str,
    incident_key: str,
    kind: str,
    severity: str,
    title: str,
    details: dict[str, Any] | None = None,
    node_uuid: str | None = None,
    transport: str | None = None,
) -> int:
    """Create an incident or refresh the existing active one."""
    return int(
        await db.fetchval(
            """INSERT INTO plugin_incidents
                       (source_plugin, incident_key, kind, severity, title,
                        details, node_uuid, transport)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::uuid,$8)
                   ON CONFLICT (source_plugin, incident_key)
                       WHERE status IN ('open', 'acknowledged')
                   DO UPDATE SET kind=EXCLUDED.kind,
                                 severity=EXCLUDED.severity,
                                 title=EXCLUDED.title,
                                 details=EXCLUDED.details,
                                 node_uuid=EXCLUDED.node_uuid,
                                 transport=EXCLUDED.transport,
                                 updated_at=NOW()
                   RETURNING id""",
            source_plugin,
            incident_key,
            kind,
            severity,
            title,
            json.dumps(details or {}, ensure_ascii=False, default=str),
            node_uuid,
            transport,
        )
        or 0
    )


async def resolve(db, *, source_plugin: str, incident_key: str) -> bool:
    result = await db.execute(
        """UPDATE plugin_incidents
              SET status='resolved', resolved_at=NOW(), updated_at=NOW()
            WHERE source_plugin=$1 AND incident_key=$2
              AND status IN ('open', 'acknowledged')""",
        source_plugin,
        incident_key,
    )
    return str(result).endswith(" 1")


def _public(row) -> dict[str, Any]:
    details = row["details"]
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = {}
    return {
        "id": int(row["id"]),
        "source_plugin": row["source_plugin"],
        "kind": row["kind"],
        "severity": row["severity"],
        "title": row["title"],
        "details": details or {},
        "node_uuid": str(row["node_uuid"]) if row["node_uuid"] else None,
        "transport": row["transport"],
        "status": row["status"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
    }


async def active_for_nodes(db, node_uuids: Iterable[str]) -> list[dict[str, Any]]:
    values = sorted({str(value) for value in node_uuids if value})
    if not values:
        return []
    rows = await db.fetch(
        """SELECT * FROM plugin_incidents
            WHERE status IN ('open', 'acknowledged')
              AND (node_uuid = ANY($1::uuid[]) OR node_uuid IS NULL)
            ORDER BY severity='critical' DESC, severity='high' DESC, updated_at DESC""",
        values,
    )
    return [_public(row) for row in rows]


async def active_node_uuids(db) -> set[str]:
    rows = await db.fetch(
        """SELECT DISTINCT node_uuid FROM plugin_incidents
            WHERE status IN ('open', 'acknowledged') AND node_uuid IS NOT NULL"""
    )
    return {str(row["node_uuid"]) for row in rows}
