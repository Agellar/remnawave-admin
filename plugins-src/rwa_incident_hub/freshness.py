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
    return await source_status(
        db, table="subscription_request_history", timestamp_column="request_at",
        max_age_minutes=max_age_minutes,
    )


async def system_status(db) -> dict[str, Any]:
    history = await history_status(db, 10)
    radar = await source_status(
        db, table="local_block_radar_samples", timestamp_column="sampled_at",
        max_age_minutes=3,
    )
    return {"ok": bool(history["fresh"] and radar["fresh"]), "history": history, "radar": radar}
