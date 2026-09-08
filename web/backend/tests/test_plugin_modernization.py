from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from rwa_incident_hub import store as incident_store
from rwa_incident_hub import freshness as incident_freshness
from rwa_incident_hub import operations as incident_operations
from rwa_incident_hub.plugin import manifest as incident_manifest
from rwa_local_block_radar import engine as radar_engine
from rwa_retention_radar import campaigns, settings as retention_settings, store as retention_store
from rwa_smart_support import store as support_store
from rwa_smart_support import data as support_data


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
    assert resolved["require_fresh_data"] is True
    assert resolved["max_data_age_minutes"] == 10


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
    assert "plugin_incident_workflow" in incident_store.DDL
    assert "plugin_incident_events" in incident_store.DDL


def test_incident_center_manifest_is_free_and_operator_facing():
    item = incident_manifest()
    assert item.id == "incident_center"
    assert item.billing == "free"
    assert item.version == "0.1.4"
    assert item.navigation[0].path == "/plugins/incident-center"
    assert item.navigation[0].permission == ("incident_center", "view")


def test_incident_center_requires_explicit_restore_failure_evidence():
    assert incident_operations.restore_failure_evidence(
        '{"restore_failed": true, "squads_restored": false}'
    ) == {"restore_failed": True}
    assert incident_operations.restore_failure_evidence(
        {"restore_required": True, "squads_restored": False}
    ) == {"restore_required": True, "squads_restored": False}
    # In upstream 4.6.2 this lone false is also emitted when there were no
    # previous squads.  It must not become a speculative incident.
    assert incident_operations.restore_failure_evidence(
        {"squads_restored": False}
    ) is None
    assert incident_operations.restore_failure_evidence("not-json") is None


@pytest.mark.asyncio
async def test_incident_operations_are_safe_when_source_tables_are_missing():
    db = AsyncMock()
    db.fetchval.side_effect = [None, None]
    result = await incident_operations.operational_context(db)
    assert result["agent_rollout"]["source_state"] == "missing"
    assert result["audit"]["source_state"] == "missing"
    assert result["audit"]["restore_failures"] == []
    db.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_incident_operations_aggregate_rollout_restarts_and_throttle_audit():
    db = AsyncMock()
    db.fetchval.side_effect = ["nodes", "admin_audit_log"]
    now = datetime.now(timezone.utc)
    db.fetch.side_effect = [
        [
            {
                "node_uuid": "node-1",
                "name": "ready",
                "agent_version": incident_operations.MIN_COMPATIBLE_AGENT_VERSION,
            },
            {"node_uuid": "node-2", "name": "rollout", "agent_version": "1.7.2"},
        ],
        [
            {
                "id": 10, "action": "violation.throttle.add",
                "resource_id": "private-user-uuid", "details": "{}", "created_at": now,
            },
            {
                "id": 11, "action": "violation.throttle.remove",
                "resource_id": "private-user-uuid",
                "details": '{"squads_restored": false}', "created_at": now,
            },
            {
                "id": 12, "action": "violation.throttle.remove",
                "resource_id": "private-user-uuid",
                "details": '{"restore_required": true, "restore_failed": true, "squads_restored": false}',
                "created_at": now,
            },
            {
                "id": 13, "action": "node.restart", "resource_id": "node-2",
                "details": "{}", "created_at": now,
            },
        ],
    ]
    result = await incident_operations.operational_context(db)
    assert result["agent_rollout"]["compatible"] == 1
    assert result["agent_rollout"]["outdated"] == [
        {"node_name": "rollout", "agent_version": "1.7.2"}
    ]
    assert result["audit"]["throttle_added"] == 1
    assert result["audit"]["throttle_removed"] == 2
    assert result["audit"]["operator_restarts"][0]["node_name"] == "rollout"
    assert result["audit"]["restore_failures"] == [{
        "audit_event_id": 12,
        "observed_at": now.isoformat(),
        "evidence": {"restore_failed": True},
    }]
    assert "private-user-uuid" not in str(result)


@pytest.mark.asyncio
async def test_incident_reconcile_upserts_only_evidenced_restore_failures(monkeypatch):
    monkeypatch.setattr(incident_store, "ensure_schema", AsyncMock())
    monkeypatch.setattr(
        incident_operations,
        "operational_context",
        AsyncMock(return_value={
            "audit": {
                "source_state": "available",
                "restore_failures": [{
                    "audit_event_id": 42,
                    "observed_at": "2026-08-26T00:00:00+00:00",
                    "evidence": {"restore_failed": True},
                }],
            }
        }),
    )
    upsert = AsyncMock(return_value=7)
    monkeypatch.setattr(incident_store, "upsert", upsert)
    result = await incident_operations.reconcile_restore_failures(object())
    assert result == {"restore_failures": 1, "audit_source_state": "available"}
    assert upsert.await_args.kwargs["incident_key"] == "throttle-restore-audit:42"
    assert upsert.await_args.kwargs["kind"] == "throttle_restore_failed"
    assert "user_uuid" not in str(upsert.await_args.kwargs)


@pytest.mark.asyncio
async def test_freshness_circuit_breaker_marks_old_history_stale():
    db = AsyncMock()
    db.fetchval.side_effect = ["subscription_request_history", "sync_metadata"]
    db.fetchrow.side_effect = [
        {"newest_at": object(), "age_seconds": 901},
        None,
    ]
    result = await incident_freshness.history_status(db, max_age_minutes=10)
    assert result["fresh"] is False
    assert result["state"] == "stale"
    assert result["freshness_basis"] == "sync_metadata"


@pytest.mark.asyncio
async def test_incident_review_writes_an_audit_event():
    db = AsyncMock()
    db.fetchval.return_value = 1
    db.fetchrow.return_value = {
        "incident_id": 7,
        "review_label": "false_positive",
    }
    result = await incident_store.review(
        db,
        incident_id=7,
        label="false_positive",
        note="operator review",
        actor="admin",
    )
    assert result["review_label"] == "false_positive"
    event_sql = db.execute.await_args.args[0]
    assert "plugin_incident_events" in event_sql


@pytest.mark.asyncio
async def test_smart_support_marks_subscription_source_stale_after_ten_minutes():
    db = AsyncMock()
    db.fetch.return_value = []
    event_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db.fetchval.side_effect = ["subscription_request_history", "sync_metadata"]
    db.fetchrow.side_effect = [
        {"newest_at": event_at, "age_seconds": 11 * 60},
        None,
    ]
    result = await support_data.client_section(db, "user", {})
    assert result["source_stale"] is True
    assert result["source_newest_at"] == event_at


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
