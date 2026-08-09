from unittest.mock import AsyncMock

import pytest

from rwa_incident_hub import store as incident_store
from rwa_local_block_radar import engine as radar_engine
from rwa_retention_radar import campaigns, settings as retention_settings, store as retention_store
from rwa_smart_support import store as support_store


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_live_campaigns_are_server_disabled_by_default():
    resolved = await retention_settings.get_safety(FakeSettings())
    assert resolved["live_campaigns_enabled"] is False
    assert resolved["require_server_arm"] is True
    assert resolved["suppress_active_incidents"] is True


@pytest.mark.asyncio
async def test_campaign_arm_returns_raw_token_but_stores_only_hash(monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(retention_store, "issue_arm", issue)

    result = await campaigns.arm(
        object(), confirm_token="a" * 32, admin_username="operator", ttl_minutes=10
    )

    assert result["arm_token"]
    assert result["idempotency_key"]
    assert result["expires_in_seconds"] == 600
    kwargs = issue.await_args.kwargs
    assert kwargs["token_hash"] != result["arm_token"]
    assert len(kwargs["token_hash"]) == 64
    assert kwargs["idempotency_key"] == result["idempotency_key"]


@pytest.mark.asyncio
async def test_real_send_is_rejected_when_server_switch_is_off(monkeypatch):
    person = {"uuid": "00000000-0000-0000-0000-000000000001", "telegram_id": 10}
    monkeypatch.setattr(campaigns, "recipients", AsyncMock(return_value=([person], {})))
    token = campaigns.confirm_token("lapsed", "hello", 30, [10])

    with pytest.raises(ValueError, match="live_campaigns_disabled"):
        await campaigns.send(
            object(),
            object(),
            segment="lapsed",
            message_text="hello",
            token=token,
            dry_run=False,
            admin_username="operator",
            th={"discount_lapsed": 30, "offer_valid_hours": 72},
            bedolaga_cfg={},
            safety={"live_campaigns_enabled": False, "require_server_arm": True},
        )


@pytest.mark.asyncio
async def test_idempotent_retry_never_recomputes_or_resends(monkeypatch):
    existing = {
        "id": 42,
        "recipients": 3,
        "sent": 3,
        "failed": 0,
        "dry_run": False,
        "status": "sent",
        "broadcast_id": 9,
    }
    monkeypatch.setattr(
        retention_store, "campaign_by_idempotency", AsyncMock(return_value=existing)
    )
    recipients = AsyncMock()
    monkeypatch.setattr(campaigns, "recipients", recipients)

    result = await campaigns.send(
        object(),
        object(),
        segment="lapsed",
        message_text="hello",
        token="ignored-on-retry",
        dry_run=False,
        admin_username="operator",
        th={},
        bedolaga_cfg={},
        safety={"live_campaigns_enabled": True, "require_server_arm": True},
        arm_token="already-used",
        idempotency_key="same-request",
    )

    assert result["campaign_id"] == 42
    assert result["status"] == "sent"
    recipients.assert_not_awaited()


def test_incident_hub_is_infrastructure_only():
    assert "plugin_incidents" in incident_store.DDL
    assert "node_uuid" in incident_store.DDL
    assert "user_uuid" not in incident_store.DDL
    assert "ip_address" not in incident_store.DDL
    assert "telegram_id" not in incident_store.DDL


@pytest.mark.asyncio
async def test_support_feedback_is_upserted_per_session_rule_and_admin():
    db = AsyncMock()
    db.fetchval.return_value = 17
    result = await support_store.save_feedback(
        db,
        session_id=8,
        target_user_uuid="00000000-0000-0000-0000-000000000001",
        rule_id="active_infrastructure_incident",
        verdict="correct",
        comment=None,
        admin_username="operator",
    )
    assert result == 17
    sql = db.fetchval.await_args.args[0]
    assert "ON CONFLICT" in sql
    assert "updated_at=NOW()" in sql


@pytest.mark.asyncio
async def test_radar_publishes_only_infrastructure_dimensions(monkeypatch):
    publish = AsyncMock(return_value=4)
    monkeypatch.setattr(radar_engine, "upsert_incident", publish)
    ctx = type("Ctx", (), {"db": object()})()
    row = {
        "node_uuid": "00000000-0000-0000-0000-000000000010",
        "node_name": "node-a",
        "provider_name": "provider-a",
        "transport": "reality",
        "online": 2,
    }
    await radar_engine._publish_incident(ctx, row, {"online": 10})
    kwargs = publish.await_args.kwargs
    assert kwargs["incident_key"].startswith("node:")
    assert kwargs["details"]["drop_percent"] == 80
    assert "user_uuid" not in kwargs["details"]
    assert "ip" not in kwargs["details"]
