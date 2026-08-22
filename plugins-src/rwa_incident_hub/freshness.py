"""Shared data-freshness circuit breaker for local plugins."""
from __future__ import annotations

from typing import Any


async def source_status(
    db, *, table: str, timestamp_column: str, max_age_minutes: int
) -> dict[str, Any]:
    allowed = {
        ("subscription_request_history", "request_at"),
        ("local_block_radar_samples", "sampled_at"),
    }
    if (table, timestamp_column) not in allowed:
        raise ValueError("unsupported_freshness_source")
    exists = await db.fetchval("SELECT to_regclass($1)", f"public.{table}")
    if not exists:
        return {"fresh": False, "state": "missing", "newest_at": None, "age_seconds": None,
                "max_age_minutes": int(max_age_minutes)}
    row = await db.fetchrow(
        f"SELECT MAX({timestamp_column}) AS newest_at, "
        f"EXTRACT(EPOCH FROM (NOW()-MAX({timestamp_column})))::bigint AS age_seconds FROM {table}"
    )
    newest = row["newest_at"] if row else None
    age = int(row["age_seconds"]) if row and row["age_seconds"] is not None else None
    fresh = age is not None and age <= int(max_age_minutes) * 60
    return {
        "fresh": fresh,
        "state": "fresh" if fresh else ("empty" if newest is None else "stale"),
        "newest_at": newest,
        "age_seconds": age,
        "max_age_minutes": int(max_age_minutes),
    }


async def history_status(db, max_age_minutes: int = 10) -> dict[str, Any]:
    """Return authoritative subscription-history sync health.

    ``MAX(request_at)`` describes the newest *event*, not whether the sync
    worker is alive.  A quiet period with no subscription requests is valid,
    so use ``sync_metadata`` when its SRH row exists.  The event timestamp is
    still returned separately for diagnostics.  Older installations without
    metadata retain the previous event-age fallback.
    """
    event = await source_status(
        db, table="subscription_request_history", timestamp_column="request_at",
        max_age_minutes=max_age_minutes,
    )
    result = {
        **event,
        "event_newest_at": event.get("newest_at"),
        "event_age_seconds": event.get("age_seconds"),
        "last_sync_at": None,
        "sync_age_seconds": None,
        "sync_status": None,
        "records_synced": None,
        "freshness_basis": "event_timestamp",
    }

    # A missing history table is never healthy, even if a stale metadata row
    # from an earlier installation happens to remain.
    if event.get("state") == "missing":
        return result

    metadata_exists = await db.fetchval(
        "SELECT to_regclass($1)", "public.sync_metadata"
    )
    if not metadata_exists:
        return result

    metadata = await db.fetchrow(
        """SELECT last_sync_at, sync_status, records_synced,
                  EXTRACT(EPOCH FROM (NOW()-last_sync_at))::bigint
                      AS sync_age_seconds
             FROM public.sync_metadata
            WHERE key=$1""",
        "subscription_request_history",
    )
    if not metadata:
        # Once the metadata table exists, the missing source row means the
        # worker has not completed a trustworthy sync yet.  Falling back to a
        # recent event here would briefly fail open after a fresh install or a
        # damaged metadata row.
        result.update(
            {
                "fresh": False,
                "state": "stale",
                "freshness_basis": "sync_metadata",
            }
        )
        return result

    last_sync_at = metadata["last_sync_at"]
    sync_age = (
        int(metadata["sync_age_seconds"])
        if metadata["sync_age_seconds"] is not None
        else None
    )
    sync_status = str(metadata["sync_status"] or "").strip().lower()
    fresh = bool(
        sync_status == "success"
        and sync_age is not None
        and sync_age >= 0
        and sync_age <= int(max_age_minutes) * 60
    )

    result.update(
        {
            "fresh": fresh,
            # Keep the public state vocabulary stable.  ``sync_status`` below
            # distinguishes an explicit error from an old successful sync.
            "state": "fresh" if fresh else "stale",
            # Backward-compatible display aliases now reflect the
            # authoritative clock; event age has dedicated fields above.
            "newest_at": last_sync_at,
            "age_seconds": sync_age,
            "last_sync_at": last_sync_at,
            "sync_age_seconds": sync_age,
            "sync_status": sync_status or None,
            "records_synced": int(metadata["records_synced"] or 0),
            "freshness_basis": "sync_metadata",
        }
    )
    return result


async def system_status(db) -> dict[str, Any]:
    history = await history_status(db, 10)
    radar = await source_status(
        db, table="local_block_radar_samples", timestamp_column="sampled_at",
        max_age_minutes=3,
    )
    return {"ok": bool(history["fresh"] and radar["fresh"]), "history": history, "radar": radar}
