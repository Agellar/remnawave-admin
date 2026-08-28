"""Regression gates for the 4.6.3 time-window/pagination integration."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from web.backend.core.user_timeline import TIMELINE_SQL, user_timeline_page

USER_ID = "00000000-0000-4000-8000-000000000001"


def database(rows):
    conn = SimpleNamespace(fetch=AsyncMock(return_value=rows))

    @asynccontextmanager
    async def acquire():
        yield conn

    return SimpleNamespace(is_connected=True, acquire=acquire), conn


@pytest.mark.asyncio
async def test_pagination_uses_one_statement_and_real_total():
    now = datetime.now(timezone.utc)
    db, conn = database([{
        "total": 521, "kind": "violation", "ts": now,
        "payload": '{"id": 1, "score": 0, "action": "annulled", "reasons": []}',
    }])
    result = await user_timeline_page(db, USER_ID, 1, 7, 50)
    assert result["total"] == 521 and result["pages"] == 11
    assert result["items"][0]["action"] == "annulled"
    assert result["items"][0]["severity"] == "low"
    conn.fetch.assert_awaited_once_with(TIMELINE_SQL, USER_ID, 1, 50, 300)


@pytest.mark.asyncio
async def test_page_beyond_end_still_has_total():
    db, _ = database([{"total": 521, "kind": None, "ts": None, "payload": None}])
    result = await user_timeline_page(db, USER_ID, 30, 15, 50)
    assert result["items"] == [] and result["total"] == 521 and result["pages"] == 11


def test_all_sources_have_same_date_window_and_stable_order():
    assert TIMELINE_SQL.count("NOW() - make_interval(days => $2)") == 3
    assert TIMELINE_SQL.count("<= NOW()") == 3
    assert "ORDER BY ts DESC, kind, tie" in TIMELINE_SQL
    assert "LIMIT 300" not in TIMELINE_SQL and "LIMIT 100" not in TIMELINE_SQL
    assert "coalesce(v.action_taken, v.recommended_action)" in TIMELINE_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("days,page,per_page", [(0, 1, 50), (366, 1, 50), (1, 0, 50), (1, 1, 501)])
async def test_invalid_bounds_rejected_before_database(days, page, per_page):
    db, conn = database([])
    with pytest.raises(ValueError):
        await user_timeline_page(db, USER_ID, days, page, per_page)
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_is_checked_before_query(app, client):
    with patch("web.backend.core.rbac.get_visible_user_uuids", AsyncMock(return_value=set())), \
         patch("web.backend.core.user_timeline.user_timeline_page", AsyncMock()) as query:
        response = await client.get(f"/api/v2/violations/user/{USER_ID}/timeline")
    assert response.status_code == 403
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_failure_is_not_empty_history(app, client):
    with patch("web.backend.core.rbac.get_visible_user_uuids", AsyncMock(return_value=None)), \
         patch("web.backend.core.user_timeline.user_timeline_page", AsyncMock(side_effect=RuntimeError("offline"))):
        response = await client.get(f"/api/v2/violations/user/{USER_ID}/timeline")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_requested_page_reaches_query(app, client):
    payload = {"items": [], "total": 400, "page": 8, "per_page": 50, "pages": 8}
    with patch("web.backend.core.rbac.get_visible_user_uuids", AsyncMock(return_value={USER_ID})), \
         patch("web.backend.core.user_timeline.user_timeline_page", AsyncMock(return_value=payload)) as query:
        response = await client.get(f"/api/v2/violations/user/{USER_ID}/timeline?days=1&page=8&per_page=50")
    assert response.status_code == 200 and response.json() == payload
    assert query.await_args.args[1:] == (USER_ID, 1, 8, 50)
