"""Admin 4.7.1 regressions for session-start versus live-activity semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from rwa_local_block_radar import __version__ as block_version
from rwa_local_block_radar import engine as block_engine
from rwa_retention_radar import __version__ as retention_version
from rwa_retention_radar import campaigns
from rwa_smart_support import __version__ as support_version
from rwa_smart_support import data as support_data
from rwa_smart_support import settings as support_settings


USER_A = "00000000-0000-0000-0000-00000000000a"
USER_B = "00000000-0000-0000-0000-00000000000b"
USER_C = "00000000-0000-0000-0000-00000000000c"
USER_D = "00000000-0000-0000-0000-00000000000d"
NODE_A = "10000000-0000-0000-0000-00000000000a"
NODE_B = "10000000-0000-0000-0000-00000000000b"


def test_only_materially_changed_plugins_have_471_versions():
    assert support_version == "1.4.6"
    assert retention_version == "1.2.6"
    assert block_version == "0.7.6"


def _node_row(node_uuid: str, now: datetime, **values):
    return {
        "uuid": node_uuid,
        "name": values.get("name", "node"),
        "address": "node.invalid",
        "is_connected": True,
        "is_disabled": False,
        "cpu_usage": 10,
        "memory_usage": 20,
        "disk_usage": 30,
        "metrics_updated_at": now,
        "history_last_seen": values.get("history_last_seen", now),
        "active_here": values.get("active_here", False),
        "online_at": values.get("online_at"),
        "live_node_uuid": values.get("live_node_uuid"),
    }


@pytest.mark.asyncio
async def test_support_nodes_keep_long_lived_current_session_and_recent_history(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(support_data, "_now", lambda: now)
    db = AsyncMock()
    db.fetch.return_value = [
        _node_row(
            NODE_B,
            now,
            name="live-long-session",
            active_here=False,
            online_at=(now - timedelta(minutes=2)).isoformat(),
            live_node_uuid=NODE_B.upper(),
            history_last_seen=None,
        ),
        _node_row(
            NODE_A,
            now,
            name="recent-history",
            active_here=True,
            online_at=(now - timedelta(minutes=2)).isoformat(),
            live_node_uuid=NODE_B,
        ),
    ]

    result = await support_data.nodes_section(db, USER_A)

    assert [item["name"] for item in result] == ["live-long-session", "recent-history"]
    assert [item["user_active_here"] for item in result] == [True, False]
    sql = db.fetch.await_args.args[0]
    assert "lastConnectedNodeUuid" in sql and "onlineAt" in sql
    assert "LEFT JOIN touched" in sql
    assert "lower(n.uuid::text) = lower(" in sql
    assert "disconnected_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_support_nodes_ignore_malformed_or_stale_panel_activity(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(support_data, "_now", lambda: now)
    db = AsyncMock()
    db.fetch.return_value = [
        _node_row(
            NODE_A,
            now,
            online_at="not-a-date",
            live_node_uuid=NODE_A,
            history_last_seen=None,
        ),
        _node_row(
            NODE_B,
            now,
            online_at=(now - timedelta(hours=2)).isoformat(),
            live_node_uuid=NODE_B,
            history_last_seen=None,
        ),
        _node_row(
            "10000000-0000-0000-0000-00000000000c",
            now,
            online_at="not-a-date",
            live_node_uuid=NODE_A,
            history_last_seen=now,
        ),
    ]

    result = await support_data.nodes_section(db, USER_A)

    assert [item["user_active_here"] for item in result] == [False]


@pytest.mark.asyncio
async def test_support_cluster_population_uses_panel_node_online_counter():
    db = AsyncMock()
    db.fetch.return_value = [{
        "node_uuid": NODE_A,
        "name": "node-a",
        "affected": 5,
        "members": [USER_A, USER_B],
        "total_users": 10,
    }]

    clusters = await support_data.compute_clusters(db, support_settings.DEFAULT_THRESHOLDS)

    assert len(clusters) == 1
    assert clusters[0]["total_users"] == 10
    assert db.fetch.await_count == 1
    sql = db.fetch.await_args.args[0]
    assert "n.users_online" in sql
    assert "COUNT(DISTINCT user_uuid)::int AS total_users" not in sql


@pytest.mark.parametrize(
    ("affected", "total_users", "expected"),
    [(4, 10, True), (4, None, False), (4, 9, False), (11, 10, True)],
)
@pytest.mark.asyncio
async def test_support_cluster_share_boundaries_and_empty_population(
    affected, total_users, expected
):
    thresholds = dict(support_settings.DEFAULT_THRESHOLDS)
    thresholds.update({
        "cluster_node_min_affected": 4.0,
        "cluster_node_min_total_users": 10.0,
        "cluster_node_min_share": 0.4,
    })
    db = AsyncMock()
    db.fetch.return_value = [{
        "node_uuid": NODE_A,
        "name": "node-a",
        "affected": affected,
        "members": [USER_A],
        "total_users": total_users,
    }]

    clusters = await support_data.compute_clusters(db, thresholds)

    assert bool(clusters) is expected


@pytest.mark.asyncio
async def test_retention_incident_suppression_combines_current_and_recent(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(campaigns, "active_node_uuids", AsyncMock(return_value={NODE_A}))
    db = AsyncMock()
    db.fetch.return_value = [
        {"user_uuid": USER_A, "recent_connection": True,
         "online_at": now.isoformat(), "live_node_uuid": NODE_A},
        {"user_uuid": USER_B, "recent_connection": False,
         "online_at": (now - timedelta(minutes=2)).isoformat(),
         "live_node_uuid": NODE_A.upper()},
        {"user_uuid": USER_C, "recent_connection": False,
         "online_at": "broken", "live_node_uuid": NODE_A},
        {"user_uuid": USER_D, "recent_connection": False,
         "online_at": now.isoformat(), "live_node_uuid": NODE_B},
    ]
    safety = {"suppress_active_incidents": True, "incident_lookback_minutes": 60}

    affected = await campaigns._incident_affected_users(
        db, safety, [USER_A, USER_B, USER_C, USER_D]
    )

    assert affected == {USER_A, USER_B}
    sql, nodes, minutes, users = db.fetch.await_args.args
    assert (nodes, minutes, users) == (
        [NODE_A], 60, [USER_A, USER_B, USER_C, USER_D]
    )
    assert "EXISTS" in sql and "lastConnectedNodeUuid" in sql and "onlineAt" in sql


def test_retention_current_activity_rejects_null_malformed_and_future_values():
    now = datetime.now(timezone.utc)
    assert campaigns._recent_panel_activity(None, 60) is False
    assert campaigns._recent_panel_activity("broken", 60) is False
    assert campaigns._recent_panel_activity(
        (now + timedelta(minutes=5)).isoformat(), 60
    ) is False


def test_block_radar_live_pairs_reject_malformed_stale_and_unknown_nodes():
    now = datetime.now(timezone.utc)
    rows = [
        {"user_uuid": USER_A, "live_node_uuid": NODE_A,
         "online_at": (now - timedelta(minutes=2)).isoformat()},
        {"user_uuid": USER_B, "live_node_uuid": NODE_A, "online_at": "broken"},
        {"user_uuid": "broken", "live_node_uuid": NODE_A,
         "online_at": now.isoformat()},
        {"user_uuid": USER_C, "live_node_uuid": NODE_A,
         "online_at": (now - timedelta(hours=2)).isoformat()},
        {"user_uuid": USER_D, "live_node_uuid": NODE_B,
         "online_at": now.isoformat()},
    ]

    assert block_engine._live_user_node_pairs(rows, {NODE_A}, 10) == [(USER_A, NODE_A)]


@pytest.mark.asyncio
async def test_block_radar_transport_query_prefers_current_same_node_evidence():
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.fetch.side_effect = [
        [{
            "node_uuid": NODE_A,
            "node_name": "node-a",
            "online": 7,
            "node_alive": True,
            "agent_version": "1.8.0",
            "provider_name": "provider-a",
        }],
        [
            {"user_uuid": USER_A, "online_at": now.isoformat(),
             "live_node_uuid": NODE_A},
            {"user_uuid": "broken", "online_at": now.isoformat(),
             "live_node_uuid": NODE_A},
        ],
        [{"node_uuid": NODE_A, "inbound_tag": "vless-reality", "hits": 1}],
    ]

    result = await block_engine.current_nodes(db, 10)

    assert result[0]["transport"] == "reality"
    tag_sql, minutes, users, nodes = db.fetch.await_args_list[2].args
    assert (minutes, users, nodes) == (10, [USER_A], [NODE_A])
    assert "live_latest" in tag_sql
    assert "p.user_uuid = c.user_uuid AND p.node_uuid = c.node_uuid" in tag_sql
    assert "NOT EXISTS" in tag_sql
    assert "ORDER BY (live_hits > 0) DESC" in tag_sql
    assert "c.connected_at DESC, c.id DESC" in tag_sql


@pytest.mark.asyncio
async def test_block_radar_keeps_bounded_recent_transport_as_explicit_fallback():
    db = AsyncMock()
    db.fetch.side_effect = [
        [{
            "node_uuid": NODE_A,
            "node_name": "node-a",
            "online": 0,
            "node_alive": True,
            "agent_version": "1.8.0",
            "provider_name": "provider-a",
        }],
        [],
        [{"node_uuid": NODE_A, "inbound_tag": "vless-ws", "hits": 2}],
    ]

    result = await block_engine.current_nodes(db, 10)

    assert result[0]["transport"] == "ws"
    _, _, users, nodes = db.fetch.await_args_list[2].args
    assert users == [] and nodes == []
