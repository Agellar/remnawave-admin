"""Focused access-scope and no-send safety checks for Retention Radar."""
from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from rwa_retention_radar import api, campaigns, data, scope, store


USER_A = "00000000-0000-0000-0000-00000000000a"
USER_B = "00000000-0000-0000-0000-00000000000b"


@pytest.mark.asyncio
async def test_schema_split_never_executes_comment_only_fragments():
    db = AsyncMock()

    await store.ensure_schema(db)

    statements = [call.args[0] for call in db.execute.await_args_list]
    executable = [
        "\n".join(
            line for line in statement.splitlines()
            if not line.lstrip().startswith("--")
        ).strip()
        for statement in statements
    ]
    assert executable
    assert all(executable)
    assert all(re.match(r"^(CREATE|ALTER)\b", statement) for statement in executable)


def _ctx(db=None):
    return SimpleNamespace(
        db=db or object(),
        settings=object(),
        logger=MagicMock(),
        telemetry=SimpleNamespace(count=MagicMock()),
    )


@pytest.mark.asyncio
async def test_regular_admin_scope_resolution_fails_closed(monkeypatch):
    admin = SimpleNamespace(account_id=17, role="operator")
    ctx = _ctx()

    async def broken_provider(_account_id, _role):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(scope, "_provider", lambda: broken_provider)

    assert await scope.resolve_visible_user_uuids(ctx, admin) == frozenset()
    ctx.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_none_scope_remains_unrestricted_and_uuid_scope_is_normalised(monkeypatch):
    admin = SimpleNamespace(account_id=17, role="operator")
    ctx = _ctx()

    async def unrestricted(_account_id, _role):
        return None

    monkeypatch.setattr(scope, "_provider", lambda: unrestricted)
    assert await scope.resolve_visible_user_uuids(ctx, admin) is None

    async def restricted(_account_id, _role):
        return {USER_A.upper(), "not-a-uuid"}

    monkeypatch.setattr(scope, "_provider", lambda: restricted)
    assert await scope.resolve_visible_user_uuids(ctx, admin) == frozenset({USER_A})


@pytest.mark.asyncio
async def test_segment_count_list_totals_and_attention_bind_user_scope():
    db = AsyncMock()
    db.fetchval.return_value = 1
    db.fetch.return_value = []
    db.fetchrow.return_value = {"total": 1, "active": 1}
    visible = frozenset({USER_A})

    await data.count_segment(db, "lapsed", {"lapsed_days": 30}, visible)
    count_sql, *count_args = db.fetchval.await_args.args
    assert "uuid = ANY($2::uuid[])" in count_sql
    assert count_args[-1] == [USER_A]

    await data.list_segment(
        db,
        "lapsed",
        {"lapsed_days": 30},
        50,
        0,
        visible_user_uuids=visible,
    )
    list_sql, *list_args = db.fetch.await_args.args
    assert "uuid = ANY($2::uuid[])" in list_sql
    assert list_args[-3] == [USER_A]

    await data.totals(db, visible)
    totals_sql, *totals_args = db.fetchrow.await_args.args
    assert "uuid = ANY($1::uuid[])" in totals_sql
    assert totals_args == [[USER_A]]

    await data.attention(
        db,
        {"expiring_days": 5, "expiring_risk_days": 2},
        visible_user_uuids=visible,
    )
    attention_sql, *attention_args = db.fetch.await_args.args
    assert "uuid = ANY($3::uuid[])" in attention_sql
    assert attention_args[-2] == [USER_A]


@pytest.mark.asyncio
async def test_segment_list_and_csv_export_forward_the_resolved_scope(monkeypatch):
    visible = frozenset({USER_A})
    ctx = _ctx()
    admin = SimpleNamespace(account_id=17, role="operator")
    resolve = AsyncMock(return_value=visible)
    list_segment = AsyncMock(return_value=[])
    count_segment = AsyncMock(return_value=0)
    monkeypatch.setattr(api, "resolve_visible_user_uuids", resolve)
    monkeypatch.setattr(
        api.settings_mod,
        "get_thresholds",
        AsyncMock(return_value={"lapsed_days": 30}),
    )
    monkeypatch.setattr(api.data, "list_segment", list_segment)
    monkeypatch.setattr(api.data, "count_segment", count_segment)

    router = api.build_router(ctx)
    segment_endpoint = next(
        route.endpoint for route in router.routes if route.path == "/segment/{key}"
    )
    export_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/segment/{key}/export"
    )

    await segment_endpoint(key="lapsed", limit=50, offset=0, admin=admin)
    await export_endpoint(key="lapsed", admin=admin)

    assert list_segment.await_count == 2
    assert all(
        call.kwargs["visible_user_uuids"] == visible
        for call in list_segment.await_args_list
    )
    count_segment.assert_awaited_once_with(
        ctx.db, "lapsed", {"lapsed_days": 30}, visible
    )


@pytest.mark.asyncio
async def test_scoped_overview_never_mixes_global_history(monkeypatch):
    visible = frozenset({USER_A})
    ctx = _ctx()
    thresholds = {
        "trend_days": 14,
        "expiring_days": 5,
        "expiring_risk_days": 2,
    }
    counts = {key: index for index, key in enumerate(data.SEGMENTS, start=1)}

    monkeypatch.setattr(api.settings_mod, "get_thresholds", AsyncMock(return_value=thresholds))
    monkeypatch.setattr(
        api.settings_mod,
        "get_safety",
        AsyncMock(return_value={"max_data_age_minutes": 10}),
    )
    count_mock = AsyncMock(return_value=counts)
    totals_mock = AsyncMock(return_value={"total": 4, "active": 3})
    attention_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(api.data, "counts", count_mock)
    monkeypatch.setattr(api.data, "totals", totals_mock)
    monkeypatch.setattr(api.data, "attention", attention_mock)
    previous = AsyncMock(return_value=99)
    trend = AsyncMock(return_value=[{"day": "2026-08-22", "users_count": 99}])
    first_day = AsyncMock(return_value="2026-08-01")
    monkeypatch.setattr(api.store, "previous_value", previous)
    monkeypatch.setattr(api.store, "trend", trend)
    monkeypatch.setattr(api.store, "first_day", first_day)
    monkeypatch.setattr(api, "history_status", AsyncMock(return_value={"fresh": True}))

    result = await api._overview_payload(ctx, visible)

    count_mock.assert_awaited_once_with(ctx.db, thresholds, visible)
    totals_mock.assert_awaited_once_with(ctx.db, visible)
    assert attention_mock.await_args.kwargs["visible_user_uuids"] == visible
    previous.assert_not_awaited()
    trend.assert_not_awaited()
    first_day.assert_not_awaited()
    assert result.history_since is None
    assert all(card.delta is None and card.trend == [] for card in result.segments)


def test_unrestricted_confirmation_token_keeps_legacy_format():
    payload = "lapsed|hello|30|10,20"
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    assert campaigns.confirm_token("lapsed", "hello", 30, [20, 10]) == expected


@pytest.mark.asyncio
async def test_scope_change_invalidates_preview_before_any_send_side_effect(monkeypatch):
    person = {"uuid": USER_A, "telegram_id": 10}
    recipient_lookup = AsyncMock(return_value=([person], {}))
    monkeypatch.setattr(campaigns, "recipients", recipient_lookup)
    ctx = _ctx()
    thresholds = {"discount_lapsed": 30, "offer_valid_hours": 72}

    preview = await campaigns.preview(
        object(),
        ctx,
        "lapsed",
        thresholds,
        custom_text="hello",
        visible_user_uuids=frozenset({USER_A}),
    )

    # B is not in this segment, so the actual audience is still identical.
    # The full visibility boundary nevertheless changed and must stale the token.
    open_campaign = AsyncMock()
    consume_arm = AsyncMock()
    create_offer = AsyncMock()
    send_broadcast = AsyncMock()
    monkeypatch.setattr(store, "open_campaign", open_campaign)
    monkeypatch.setattr(store, "consume_arm", consume_arm)
    monkeypatch.setattr(campaigns.bedolaga, "create_offer", create_offer)
    monkeypatch.setattr(campaigns.bedolaga, "send_broadcast", send_broadcast)

    with pytest.raises(ValueError, match="stale_confirmation"):
        await campaigns.send(
            object(),
            ctx,
            segment="lapsed",
            message_text="hello",
            token=preview["confirm_token"],
            dry_run=False,
            admin_username="renamed-operator",
            admin_account_id=17,
            th=thresholds,
            bedolaga_cfg={"token": "not-used"},
            safety={"live_campaigns_enabled": True, "require_server_arm": True},
            arm_token="not-used",
            idempotency_key="not-used",
            visible_user_uuids=frozenset({USER_A, USER_B}),
        )

    open_campaign.assert_not_awaited()
    consume_arm.assert_not_awaited()
    create_offer.assert_not_awaited()
    send_broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_is_bound_to_stable_account_id_with_legacy_fallback(monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(store, "issue_arm", issue)

    await campaigns.arm(
        object(),
        confirm_token="a" * 32,
        admin_account_id=17,
        admin_username="old-name",
        ttl_minutes=10,
    )
    assert issue.await_args.kwargs["admin_account_id"] == 17

    db = AsyncMock()
    db.fetchval.return_value = "token-hash"
    assert await store.consume_arm(
        db,
        token_hash="token-hash",
        confirm_token="a" * 32,
        idempotency_key="request-id",
        admin_account_id=17,
        admin_username="new-name",
    )
    sql, *args = db.fetchval.await_args.args
    assert "admin_account_id=$4" in sql
    assert "admin_account_id IS NULL" in sql
    assert args[-2:] == [17, "new-name"]
    assert "ADD COLUMN IF NOT EXISTS admin_account_id BIGINT" in store.DDL
