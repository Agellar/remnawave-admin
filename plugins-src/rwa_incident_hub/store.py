"""Small privacy-safe incident bus backed by the panel database.

Only infrastructure dimensions are stored here. User UUIDs, IP addresses and
Telegram identifiers deliberately do not belong in the shared incident table.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS plugin_incident_workflow (
    incident_id BIGINT PRIMARY KEY REFERENCES plugin_incidents(id) ON DELETE CASCADE,
    workflow_status TEXT NOT NULL DEFAULT 'new',
    snoozed_until TIMESTAMPTZ,
    assigned_to TEXT,
    review_label TEXT,
    review_note TEXT,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plugin_incident_events (
    id BIGSERIAL PRIMARY KEY,
    incident_id BIGINT NOT NULL REFERENCES plugin_incidents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    review_label TEXT,
    note TEXT,
    actor TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS plugin_incident_events_incident_idx
    ON plugin_incident_events (incident_id, created_at DESC);
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
    base_status = row["status"]
    workflow_status = row.get("workflow_status") if hasattr(row, "get") else None
    snoozed_until = row.get("snoozed_until") if hasattr(row, "get") else None
    if (
        workflow_status == "snoozed"
        and snoozed_until is not None
        and snoozed_until <= datetime.now(timezone.utc)
    ):
        workflow_status = "acknowledged"
    operational_status = "resolved" if base_status == "resolved" else (workflow_status or "new")
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
        "workflow_status": workflow_status or "new",
        "operational_status": operational_status,
        "snoozed_until": snoozed_until,
        "assigned_to": row.get("assigned_to") if hasattr(row, "get") else None,
        "review_label": row.get("review_label") if hasattr(row, "get") else None,
        "review_note": row.get("review_note") if hasattr(row, "get") else None,
        "updated_by": row.get("updated_by") if hasattr(row, "get") else None,
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "resolved_at": row.get("resolved_at") if hasattr(row, "get") else None,
    }


async def list_incidents(
    db, *, active: bool | None = None, limit: int = 100, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    where = ""
    if active is True:
        where = "WHERE i.status IN ('open', 'acknowledged')"
    elif active is False:
        where = "WHERE i.status = 'resolved'"
    total = int(await db.fetchval(f"SELECT COUNT(*) FROM plugin_incidents i {where}") or 0)
    rows = await db.fetch(
        f"""SELECT i.*, w.workflow_status, w.snoozed_until, w.assigned_to,
                    w.review_label, w.review_note, w.updated_by
               FROM plugin_incidents i
          LEFT JOIN plugin_incident_workflow w ON w.incident_id=i.id
               {where}
           ORDER BY (i.status IN ('open','acknowledged')) DESC,
                    i.severity='critical' DESC, i.severity='high' DESC,
                    i.updated_at DESC LIMIT $1 OFFSET $2""",
        int(limit), int(offset),
    )
    return [_public(row) for row in rows], total


async def set_workflow(
    db, *, incident_id: int, status: str, snooze_minutes: int | None,
    assigned_to: str | None, note: str | None, actor: str | None,
) -> dict[str, Any] | None:
    current = await db.fetchrow(
        "SELECT workflow_status FROM plugin_incident_workflow WHERE incident_id=$1",
        int(incident_id),
    )
    exists = await db.fetchval("SELECT 1 FROM plugin_incidents WHERE id=$1", int(incident_id))
    if not exists:
        return None
    previous = current["workflow_status"] if current else "new"
    row = await db.fetchrow(
        """INSERT INTO plugin_incident_workflow
                   (incident_id, workflow_status, snoozed_until, assigned_to, updated_by)
               VALUES ($1,$2,
                       CASE WHEN $2='snoozed' THEN NOW()+make_interval(mins=>$3) END,
                       $4,$5)
               ON CONFLICT (incident_id) DO UPDATE SET
                   workflow_status=EXCLUDED.workflow_status,
                   snoozed_until=EXCLUDED.snoozed_until,
                   assigned_to=COALESCE(EXCLUDED.assigned_to, plugin_incident_workflow.assigned_to),
                   updated_by=EXCLUDED.updated_by, updated_at=NOW()
               RETURNING *""",
        int(incident_id), status, int(snooze_minutes or 0), assigned_to, actor,
    )
    await db.execute(
        """INSERT INTO plugin_incident_events
                   (incident_id,event_type,from_status,to_status,note,actor)
               VALUES ($1,'workflow',$2,$3,$4,$5)""",
        int(incident_id), previous, status, note, actor,
    )
    return dict(row)


async def review(
    db, *, incident_id: int, label: str, note: str | None, actor: str | None,
) -> dict[str, Any] | None:
    exists = await db.fetchval("SELECT 1 FROM plugin_incidents WHERE id=$1", int(incident_id))
    if not exists:
        return None
    row = await db.fetchrow(
        """INSERT INTO plugin_incident_workflow
                   (incident_id, review_label, review_note, updated_by)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (incident_id) DO UPDATE SET
                   review_label=EXCLUDED.review_label,
                   review_note=EXCLUDED.review_note,
                   updated_by=EXCLUDED.updated_by, updated_at=NOW()
               RETURNING *""",
        int(incident_id), label, note, actor,
    )
    await db.execute(
        """INSERT INTO plugin_incident_events
                   (incident_id,event_type,review_label,note,actor)
               VALUES ($1,'review',$2,$3,$4)""",
        int(incident_id), label, note, actor,
    )
    return dict(row)


async def quality_summary(db) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """SELECT i.source_plugin, i.kind,
                  COUNT(*)::int AS reviewed,
                  COUNT(*) FILTER (WHERE w.review_label='confirmed')::int AS confirmed,
                  COUNT(*) FILTER (WHERE w.review_label='false_positive')::int AS false_positive,
                  COUNT(*) FILTER (WHERE w.review_label='unclear')::int AS unclear
             FROM plugin_incidents i
             JOIN plugin_incident_workflow w ON w.incident_id=i.id
            WHERE w.review_label IS NOT NULL
            GROUP BY i.source_plugin,i.kind ORDER BY reviewed DESC"""
    )
    return [dict(row) for row in rows]


async def events(db, incident_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT * FROM plugin_incident_events WHERE incident_id=$1 ORDER BY created_at DESC LIMIT $2",
        int(incident_id), int(limit),
    )
    return [dict(row) for row in rows]


async def active_for_nodes(db, node_uuids: Iterable[str]) -> list[dict[str, Any]]:
    """Actionable evidence for support, excluding explicit detector mistakes.

    A snooze only mutes the operator's queue; it does not establish recovery.
    Only an explicit false-positive review removes this incident from the
    downstream evidence set. The incident and its audit history remain intact.
    """
    values = sorted({str(value) for value in node_uuids if value})
    if not values:
        return []
    rows = await db.fetch(
        """SELECT i.*, w.workflow_status, w.snoozed_until, w.assigned_to,
                   w.review_label, w.review_note, w.updated_by
              FROM plugin_incidents i
         LEFT JOIN plugin_incident_workflow w ON w.incident_id=i.id
             WHERE i.status IN ('open', 'acknowledged')
               AND w.review_label IS DISTINCT FROM 'false_positive'
               AND (i.node_uuid = ANY($1::uuid[]) OR i.node_uuid IS NULL)
             ORDER BY i.severity='critical' DESC, i.severity='high' DESC, i.updated_at DESC""",
        values,
    )
    return [_public(row) for row in rows]


async def active_node_uuids(db) -> set[str]:
    """Nodes with actionable incidents, using the same review rule as support."""
    rows = await db.fetch(
        """SELECT DISTINCT i.node_uuid FROM plugin_incidents i
         LEFT JOIN plugin_incident_workflow w ON w.incident_id=i.id
             WHERE i.status IN ('open', 'acknowledged')
               AND w.review_label IS DISTINCT FROM 'false_positive'
               AND i.node_uuid IS NOT NULL"""
    )
    return {str(row["node_uuid"]) for row in rows}
