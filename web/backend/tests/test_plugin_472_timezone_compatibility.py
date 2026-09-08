"""UTC and stale-history compatibility checks for Admin 4.7.2 plugins."""
from __future__ import annotations

import inspect

from rwa_live_flow import data as live_flow_data
from rwa_local_block_radar import engine as block_engine
from rwa_retention_radar import campaigns as retention_campaigns
from rwa_retention_radar import data as retention_data
from rwa_retention_radar import store as retention_store
from rwa_smart_support import data as support_data


FUTURE_GUARD = "NOW() + INTERVAL '30 seconds'"


def test_smart_support_quarantines_future_history_rows_in_every_recent_window():
    assert support_data._SEARCH_SELECT.count(FUTURE_GUARD) == 1
    guarded_functions = {
        support_data.search_users: 1,
        support_data.history_section: 3,
        support_data.client_section: 1,
        support_data.nodes_section: 1,
        support_data.violations_recap_section: 1,
        support_data.violations_section: 1,
        support_data.compute_clusters: 1,
    }
    for function, expected in guarded_functions.items():
        assert inspect.getsource(function).count(FUTURE_GUARD) == expected


def test_retention_uses_utc_days_and_quarantines_future_activity():
    store_source = inspect.getsource(retention_store)
    assert store_source.count("(NOW() AT TIME ZONE 'UTC')::date") == 3
    assert "CURRENT_DATE" not in store_source
    assert FUTURE_GUARD in retention_data._BASE_CTE
    timestamp_sql = retention_data._panel_timestamp_sql("raw_value")
    assert "(raw_value)::timestamptz" in timestamp_sql
    assert "(raw_value)::timestamp AT TIME ZONE 'UTC'" in timestamp_sql
    assert timestamp_sql.index("::timestamptz") < timestamp_sql.index(
        "::timestamp AT TIME ZONE 'UTC'"
    )
    assert "(Z|[+-]" in timestamp_sql
    assert "parsed_last_online" in retention_data._BASE_CTE
    assert FUTURE_GUARD in inspect.getsource(
        retention_campaigns._incident_affected_users
    )


def test_block_radar_quarantines_future_transport_and_restart_evidence():
    assert inspect.getsource(block_engine.current_nodes).count(FUTURE_GUARD) == 2
    assert inspect.getsource(block_engine.recent_restart_nodes).count(FUTURE_GUARD) == 1


def test_live_flow_quarantines_future_connection_fallback_rows():
    guard = "now() + interval '30 seconds'"
    assert inspect.getsource(live_flow_data).count(guard) == 5
