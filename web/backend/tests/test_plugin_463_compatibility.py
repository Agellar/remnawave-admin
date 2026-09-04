"""4.6.3 recurrence, dismissed-evidence and fleet compatibility safeguards."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from shared.agent_version import LATEST_AGENT_VERSION
from shared.config_service import config_service
from rwa_incident_hub import operations, store as incident_store
from rwa_retention_radar import campaigns
from rwa_smart_support import ai, data
from rwa_smart_support.schemas import ViolationRecap


USER_A = "00000000-0000-0000-0000-00000000000a"
USER_B = "00000000-0000-0000-0000-00000000000b"


@pytest.mark.parametrize(
    "value, expected",
    [
        (7, 7), ("45", 45), (365, 365), (None, 30), (True, 30),
        (0, 30), (-1, 30), (366, 30), ("unknown", 30), (float("inf"), 30),
    ],
)
def test_recap_window_uses_panel_setting_and_bounds_malformed_values(
    monkeypatch, value, expected
):
    monkeypatch.setattr(config_service, "get", lambda _key, _default: value)
    assert data.violation_recap_days() == expected


@pytest.mark.asyncio
async def test_recap_counts_complete_window_and_separates_annulled_and_resolved(monkeypatch):
    monkeypatch.setattr(data, "violation_recap_days", lambda: 45)
    last_at = datetime.now(timezone.utc)
    db = AsyncMock()
    db.fetchrow.return_value = {
        "total": 42, "unresolved": 2, "resolved": 40,
        "annulled": 9, "last_at": last_at,
    }

    recap = await data.violations_recap_section(db, USER_A)

    assert ViolationRecap(**recap).model_dump() == {
        "window_days": 45, "total": 42, "unresolved": 2,
        "resolved": 40, "annulled": 9, "last_at": last_at,
    }
    sql, user, days = db.fetchrow.await_args.args
    assert (user, days) == (USER_A, 45)
    assert "action_taken IS DISTINCT FROM 'annulled'" in sql
    assert "WHERE action_taken = 'annulled'" in sql
    assert "COALESCE(action_taken, '') = ''" in sql
    assert "COALESCE(action_taken, '') NOT IN ('', 'annulled')" in sql
    assert "LIMIT" not in sql.upper()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_recap_never_invents_recurrence(monkeypatch):
    monkeypatch.setattr(data, "violation_recap_days", lambda: 30)
    db = AsyncMock()
    db.fetchrow.return_value = None
    assert await data.violations_recap_section(db, USER_A) == {
        "window_days": 30, "total": 0, "unresolved": 0,
        "resolved": 0, "annulled": 0, "last_at": None,
    }


def test_ai_separates_full_recap_from_bounded_history_and_rejects_annulled_rows():
    report = {
        "user": {}, "history_24h": {"timeline": []}, "client": {},
        "violations_recap": {
            "window_days": 45, "total": 42, "unresolved": 2,
            "resolved": 40, "annulled": 9, "last_at": "private-timestamp",
            "private_key": "must-not-leak",
        },
        "violations_recent": [
            {"score": 70, "recommended_action": "throttle", "action_taken": None},
            {"score": 80, "recommended_action": "block_user", "action_taken": "resolved"},
            {"score": 90, "recommended_action": "block_user", "action_taken": "annulled"},
            {"score": 95, "recommended_action": "block_user", "action_taken": " Annulled "},
            "invalid-legacy-item",
        ],
    }

    context = ai.build_context(report)

    assert context["violations_recap"] == {
        "window_days": 45, "total": 42, "unresolved": 2,
        "resolved": 40, "annulled": 9,
    }
    assert context["violations_sample"] == {
        "window_days": 14, "limit": 10, "returned": 2, "is_complete_count": False,
    }
    assert "violations_14d" not in context
    assert len(context["violations_recent"]) == 2
    assert context["violations_recent"][0]["is_resolved"] is False
    assert context["violations_recent"][1]["is_resolved"] is True
    assert "must-not-leak" not in str(context)
    assert "private-timestamp" not in str(context)
    prompt = ai.build_system_prompt("русский")
    assert "annulled — ошибки детектора, а не рецидив" in prompt
    assert "ещё не разобранные сигналы" in prompt
    assert "Историческое action_taken" in prompt


def test_ai_sample_counter_matches_delivered_sample_not_database_total():
    context = ai.build_context({
        "user": {}, "history_24h": {"timeline": []}, "client": {},
        "violations_recent": [{"score": 50, "action_taken": None}] * 20,
    })
    assert len(context["violations_recent"]) == 10
    assert context["violations_sample"]["returned"] == 10
    assert context["violations_sample"]["is_complete_count"] is False
    assert context["violations_recap"] is None


def test_incident_agent_baseline_follows_official_panel_constant():
    assert operations.MIN_COMPATIBLE_AGENT_VERSION == LATEST_AGENT_VERSION


class IncidentReadDB:
    """Run the portable downstream evidence SELECT without a fake predicate."""

    def __init__(self, connection):
        self.connection = connection

    async def fetch(self, query, *params):
        return self.connection.execute(query, params).fetchall()


@pytest.mark.asyncio
async def test_reviewed_false_positive_does_not_suppress_users_but_new_episode_does():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE plugin_incidents (id INTEGER PRIMARY KEY, node_uuid TEXT, status TEXT)"
        )
        connection.execute(
            "CREATE TABLE plugin_incident_workflow "
            "(incident_id INTEGER PRIMARY KEY, review_label TEXT, workflow_status TEXT)"
        )
        connection.executemany(
            "INSERT INTO plugin_incidents VALUES (?, ?, ?)",
            [
                (1, "false-positive-only", "open"),
                (2, "confirmed", "open"),
                (3, "unclear", "open"),
                (4, "unreviewed", "open"),
                (5, "snoozed", "acknowledged"),
                (6, "resolved", "resolved"),
            ],
        )
        connection.executemany(
            "INSERT INTO plugin_incident_workflow VALUES (?, ?, ?)",
            [
                (1, "false_positive", "new"),
                (2, "confirmed", "investigating"),
                (3, "unclear", "acknowledged"),
                (5, None, "snoozed"),
            ],
        )
        db = IncidentReadDB(connection)

        assert await incident_store.active_node_uuids(db) == {
            "confirmed", "unclear", "unreviewed", "snoozed",
        }

        # Existing upsert creates a new ID after the prior episode resolves.
        # The old episode's review must not dismiss this new evidence.
        connection.execute("UPDATE plugin_incidents SET status='resolved' WHERE id=1")
        connection.execute(
            "INSERT INTO plugin_incidents VALUES (7, 'false-positive-only', 'open')"
        )
        assert "false-positive-only" in await incident_store.active_node_uuids(db)
        assert connection.execute(
            "SELECT review_label FROM plugin_incident_workflow WHERE incident_id=1"
        ).fetchone()[0] == "false_positive"


@pytest.mark.asyncio
async def test_support_uses_same_explicit_false_positive_review_filter():
    db = AsyncMock()
    db.fetch.return_value = []

    assert await incident_store.active_for_nodes(db, [USER_A]) == []

    sql, values = db.fetch.await_args.args
    assert values == [USER_A]
    assert "LEFT JOIN plugin_incident_workflow w ON w.incident_id=i.id" in sql
    assert "w.review_label IS DISTINCT FROM 'false_positive'" in sql
    assert "workflow_status !=" not in sql
    assert "snoozed_until >" not in sql
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_outage_scan_is_bounded_to_current_recipient_scope(monkeypatch):
    monkeypatch.setattr(campaigns, "active_node_uuids", AsyncMock(return_value={USER_B}))
    db = AsyncMock()
    db.fetch.return_value = [{
        "user_uuid": USER_A,
        "recent_connection": True,
        "online_at": None,
        "live_node_uuid": None,
    }]
    safety = {"suppress_active_incidents": True, "incident_lookback_minutes": 60}

    assert await campaigns._incident_affected_users(db, safety, [USER_A, USER_A]) == {USER_A}

    sql, nodes, minutes, candidates = db.fetch.await_args.args
    assert (nodes, minutes, candidates) == ([USER_B], 60, [USER_A])
    assert "u.uuid = ANY($3::uuid[])" in sql
    assert "c.connected_at" in sql
    assert "violations" not in sql
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_without_candidates_or_actionable_incidents_never_scans_history(monkeypatch):
    active = AsyncMock(return_value=set())
    monkeypatch.setattr(campaigns, "active_node_uuids", active)
    db = AsyncMock()
    safety = {"suppress_active_incidents": True, "incident_lookback_minutes": 60}

    assert await campaigns._incident_affected_users(db, safety, []) == set()
    active.assert_not_awaited()
    assert await campaigns._incident_affected_users(db, safety, [USER_A]) == set()
    active.assert_awaited_once()
    db.fetch.assert_not_awaited()
