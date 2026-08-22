"""Authoritative sync freshness for Incident, Retention and Smart Support."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from rwa_incident_hub.freshness import history_status
from rwa_smart_support import data as support_data


def _db_with_history_status(
    *,
    event_age_seconds: int,
    sync_age_seconds: int | None,
    sync_status: str | None,
    records_synced: int = 0,
):
    now = datetime.now(timezone.utc)
    event_at = now - timedelta(seconds=event_age_seconds)
    sync_at = (
        now - timedelta(seconds=sync_age_seconds)
        if sync_age_seconds is not None
        else None
    )
    db = AsyncMock()
    db.fetchval.side_effect = ["subscription_request_history", "sync_metadata"]
    db.fetchrow.side_effect = [
        {"newest_at": event_at, "age_seconds": event_age_seconds},
        {
            "last_sync_at": sync_at,
            "sync_status": sync_status,
            "records_synced": records_synced,
            "sync_age_seconds": sync_age_seconds,
        },
    ]
    return db, event_at, sync_at


@pytest.mark.asyncio
async def test_recent_successful_sync_is_fresh_when_no_new_events_arrived():
    db, event_at, sync_at = _db_with_history_status(
        event_age_seconds=16 * 60,
        sync_age_seconds=45,
        sync_status="success",
        records_synced=22,
    )

    result = await history_status(db, max_age_minutes=10)

    assert result["fresh"] is True
    assert result["state"] == "fresh"
    assert result["freshness_basis"] == "sync_metadata"
    assert result["newest_at"] == sync_at
    assert result["age_seconds"] == 45
    assert result["last_sync_at"] == sync_at
    assert result["sync_status"] == "success"
    assert result["records_synced"] == 22
    assert result["event_newest_at"] == event_at
    assert result["event_age_seconds"] == 16 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sync_status", "sync_age_seconds"),
    [("error", 30), ("success", 601), ("success", -1), (None, 30)],
)
async def test_metadata_error_unknown_or_old_success_fails_closed(
    sync_status, sync_age_seconds
):
    db, _, _ = _db_with_history_status(
        event_age_seconds=15,
        sync_age_seconds=sync_age_seconds,
        sync_status=sync_status,
    )

    result = await history_status(db, max_age_minutes=10)

    assert result["fresh"] is False
    assert result["state"] == "stale"
    assert result["freshness_basis"] == "sync_metadata"


@pytest.mark.asyncio
async def test_missing_metadata_row_fails_closed_when_table_exists():
    event_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    db = AsyncMock()
    db.fetchval.side_effect = ["subscription_request_history", "sync_metadata"]
    db.fetchrow.side_effect = [
        {"newest_at": event_at, "age_seconds": 90},
        None,
    ]

    result = await history_status(db, max_age_minutes=10)

    assert result["fresh"] is False
    assert result["state"] == "stale"
    assert result["freshness_basis"] == "sync_metadata"
    assert result["newest_at"] == event_at
    assert result["event_newest_at"] == event_at
    assert result["last_sync_at"] is None


@pytest.mark.asyncio
async def test_missing_metadata_table_keeps_legacy_event_fallback():
    event_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    db = AsyncMock()
    db.fetchval.side_effect = ["subscription_request_history", None]
    db.fetchrow.return_value = {"newest_at": event_at, "age_seconds": 90}

    result = await history_status(db, max_age_minutes=10)

    assert result["fresh"] is True
    assert result["freshness_basis"] == "event_timestamp"
    assert result["event_newest_at"] == event_at


@pytest.mark.asyncio
async def test_smart_support_uses_sync_health_not_global_event_recency():
    db, event_at, _ = _db_with_history_status(
        event_age_seconds=2 * 60 * 60,
        sync_age_seconds=20,
        sync_status="success",
        records_synced=0,
    )
    db.fetch.return_value = [
        {"user_agent": "Happ/4.11.0/ios", "request_at": event_at}
    ]

    result = await support_data.client_section(db, "user", {"happ": "4.11.0"})

    assert result["source_stale"] is False
    assert result["source_newest_at"] == event_at
    assert result["days_since_last_request"] == 0
