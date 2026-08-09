"""Regression tests for the self-hosted, local-only Block Radar."""
from __future__ import annotations

import pytest

from rwa_local_block_radar import engine, settings, store
from rwa_local_block_radar.api import _local_id
from rwa_local_block_radar.plugin import manifest


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("VLESS_REALITY", "reality"),
        ("reality-xhttp", "reality+xhttp"),
        ("vless-splithttp", "xhttp"),
        ("vless-ws", "ws"),
        ("grpc-main", "grpc"),
        ("", "mixed"),
    ],
)
def test_transport_classification(tag, expected):
    assert engine.classify_transport(tag) == expected


def test_thresholds_are_clamped_to_safe_ranges():
    result = settings._validated(
        {
            "dip_frac": 0.01,
            "dip_min_users": 0,
            "dip_confirm_ticks": 1,
            "dip_history_days": 999,
            "online_window_minutes": 0,
        }
    )
    assert result["dip_frac"] == 0.30
    assert result["dip_min_users"] == 3
    assert result["dip_confirm_ticks"] == 2
    assert result["dip_history_days"] == 30
    assert result["online_window_minutes"] == 1
    assert result["notify_enabled"] is False


def test_node_dip_requires_both_share_and_absolute_drop():
    cfg = settings._validated({"dip_frac": 0.65, "dip_min_users": 8})
    base = {"online": 100.0, "share": 0.25}
    assert engine._is_dip({"online": 20, "share": 0.05}, base, cfg) is True
    assert engine._is_dip({"online": 90, "share": 0.05}, base, cfg) is False
    assert engine._is_dip({"online": 20, "share": 0.20}, base, cfg) is False


def test_local_provider_ids_are_stable_and_not_public_asns():
    assert _local_id("UPCLOUD") == _local_id("UPCLOUD")
    assert _local_id("UPCLOUD") < 0
    assert _local_id("TIMEWEB") != _local_id("UPCLOUD")


def test_manifest_uses_builtin_block_radar_ui_without_license():
    item = manifest()
    assert item.id == "block_radar"
    assert item.billing == "free"
    assert item.navigation[0].path == "/plugins/block-radar"


def test_schema_prevents_duplicate_open_incidents():
    assert "local_block_radar_one_open_alert_idx" in store.DDL
    assert "WHERE resolved_at IS NULL" in store.DDL
