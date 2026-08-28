"""Bounded, database-paginated user timeline with one consistent time window."""
from __future__ import annotations

import json

from shared.db_schema import (
    NODES_TABLE,
    USER_CONNECTIONS_TABLE,
    USER_HWID_DEVICES_TABLE,
    VIOLATIONS_TABLE,
)
from web.backend.schemas.violation import ViolationListItem


TIMELINE_SQL = f"""
WITH events AS (
    SELECT 'violation'::text AS kind, v.detected_at AS ts, v.id::text AS tie
    FROM {VIOLATIONS_TABLE} v
    WHERE v.user_uuid = $1::uuid
      AND v.detected_at >= NOW() - make_interval(days => $2)
      AND v.detected_at <= NOW()
    UNION ALL
    SELECT 'connection', uc.connected_at, uc.id::text
    FROM {USER_CONNECTIONS_TABLE} uc
    WHERE uc.user_uuid = $1::uuid
      AND uc.connected_at >= NOW() - make_interval(days => $2)
      AND uc.connected_at <= NOW()
    UNION ALL
    SELECT 'hwid', h.created_at, h.hwid
    FROM {USER_HWID_DEVICES_TABLE} h
    WHERE h.user_uuid = $1::uuid AND h.removed_at IS NULL
      AND h.created_at >= NOW() - make_interval(days => $2)
      AND h.created_at <= NOW()
), totals AS (
    SELECT count(*) AS total FROM events
), selected AS (
    SELECT * FROM events
    ORDER BY ts DESC, kind, tie
    LIMIT $3 OFFSET $4
)
SELECT totals.total, selected.kind, selected.ts,
    CASE selected.kind
        WHEN 'violation' THEN jsonb_build_object(
            'id', v.id, 'score', coalesce(v.score, 0),
            'action', coalesce(v.action_taken, v.recommended_action),
            'reasons', coalesce(v.reasons, ARRAY[]::text[])
        )
        WHEN 'connection' THEN jsonb_build_object(
            'id', uc.id, 'ip', uc.ip_address, 'node_name', n.name,
            'disconnected_at', uc.disconnected_at,
            'platform', uc.device_info->>'platform',
            'user_agent', coalesce(uc.device_info->>'userAgent', uc.device_info->>'user_agent')
        )
        WHEN 'hwid' THEN jsonb_build_object(
            'hwid', h.hwid, 'platform', h.platform,
            'device_model', h.device_model, 'app_version', h.app_version
        )
    END AS payload
FROM totals LEFT JOIN selected ON TRUE
LEFT JOIN {VIOLATIONS_TABLE} v ON selected.kind = 'violation'
    AND v.id = CASE WHEN selected.kind = 'violation' THEN selected.tie::bigint END
LEFT JOIN {USER_CONNECTIONS_TABLE} uc ON selected.kind = 'connection'
    AND uc.id = CASE WHEN selected.kind = 'connection' THEN selected.tie::bigint END
    AND uc.connected_at = selected.ts
LEFT JOIN {NODES_TABLE} n ON n.uuid = uc.node_uuid
LEFT JOIN {USER_HWID_DEVICES_TABLE} h ON selected.kind = 'hwid'
    AND h.user_uuid = $1::uuid AND h.hwid = selected.tie
ORDER BY selected.ts DESC, selected.kind, selected.tie
"""


async def user_timeline_page(db, user_uuid: str, days: int, page: int, per_page: int) -> dict:
    """Return only a page, not the old truncated 300-connection/100-violation set.

    The caller must check the user's visibility first. Counting and selecting
    share one statement/snapshot; a page beyond the end still gets a real total.
    Only compact keys are counted/sorted; payloads and node names load after LIMIT.
    Database failure propagates so the UI never calls missing history "empty".
    """
    if not db.is_connected:
        raise RuntimeError("timeline_database_unavailable")
    if not 1 <= days <= 365 or not 1 <= per_page <= 500 or page < 1:
        raise ValueError("invalid_timeline_bounds")
    async with db.acquire() as conn:
        rows = await conn.fetch(TIMELINE_SQL, user_uuid, days, per_page, (page - 1) * per_page)
    total = int(rows[0]["total"]) if rows else 0
    items = []
    for row in rows:
        if row["kind"] is None:
            continue
        payload = row["payload"]
        item = json.loads(payload) if isinstance(payload, str) else dict(payload)
        item["type"] = row["kind"]
        item["ts"] = row["ts"].isoformat()
        if item["type"] == "violation":
            item["score"] = float(item.get("score") or 0)
            item["severity"] = ViolationListItem.get_severity(item["score"]).value
        items.append(item)
    return {
        "items": items, "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }
