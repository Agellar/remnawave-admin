"""Validated settings for the local-only radar."""
from __future__ import annotations

from typing import Any

KEY = "radar_settings"

DEFAULTS: dict[str, Any] = {
    "notify_enabled": False,
    "notify_resolved": False,
    "online_window_minutes": 10,
    "send_org_names": True,
    "dip_enabled": True,
    "dip_notify_offline": False,
    "dip_frac": 0.65,
    "dip_min_users": 8,
    "dip_confirm_ticks": 3,
    "dip_history_days": 7,
    "globalping_enabled": True,
    "node_probe_enabled": True,
    "probe_interval_seconds": 60,
    "probe_confirm_cycles": 2,
    "probe_min_ru_results": 2,
    "node_probe_vantages": 3,
    "probe_timeout_seconds": 8,
    # Sonnet is an explanatory layer over confirmed deterministic alerts.
    "ai_enabled": True,
    "ai_auto_analyze": True,
    "ai_model": "claude-sonnet-4-6",
    "ai_monthly_limit": 100,
}


async def get(settings) -> dict[str, Any]:
    stored = await settings.get(KEY, {}) or {}
    out = dict(DEFAULTS)
    if isinstance(stored, dict):
        for key in out:
            if key in stored:
                out[key] = stored[key]
    return _validated(out)


async def patch(settings, values: dict[str, Any]) -> dict[str, Any]:
    current = await get(settings)
    for key in DEFAULTS:
        if key in values:
            current[key] = values[key]
    current = _validated(current)
    await settings.set(KEY, current)
    return current


def _validated(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "notify_enabled": bool(values.get("notify_enabled", False)),
        "notify_resolved": bool(values.get("notify_resolved", False)),
        "online_window_minutes": max(1, min(60, int(values.get("online_window_minutes", 10)))),
        "send_org_names": bool(values.get("send_org_names", True)),
        "dip_enabled": bool(values.get("dip_enabled", True)),
        "dip_notify_offline": bool(values.get("dip_notify_offline", False)),
        "dip_frac": max(0.30, min(0.90, float(values.get("dip_frac", 0.65)))),
        "dip_min_users": max(3, min(1000, int(values.get("dip_min_users", 8)))),
        "dip_confirm_ticks": max(2, min(12, int(values.get("dip_confirm_ticks", 3)))),
        "dip_history_days": max(1, min(30, int(values.get("dip_history_days", 7)))),
        "globalping_enabled": bool(values.get("globalping_enabled", True)),
        "node_probe_enabled": bool(values.get("node_probe_enabled", True)),
        "probe_interval_seconds": max(60, min(3600, int(values.get("probe_interval_seconds", 60)))),
        "probe_confirm_cycles": max(2, min(6, int(values.get("probe_confirm_cycles", 2)))),
        "probe_min_ru_results": max(2, min(5, int(values.get("probe_min_ru_results", 2)))),
        "node_probe_vantages": max(1, min(5, int(values.get("node_probe_vantages", 3)))),
        "probe_timeout_seconds": max(3, min(15, int(values.get("probe_timeout_seconds", 8)))),
        "ai_enabled": bool(values.get("ai_enabled", True)),
        "ai_auto_analyze": bool(values.get("ai_auto_analyze", True)),
        # This plugin is deliberately calibrated for Sonnet. A malformed or
        # non-Sonnet override falls back instead of silently changing model family.
        "ai_model": (
            str(values.get("ai_model") or "claude-sonnet-4-6").strip()
            if str(values.get("ai_model") or "").strip().startswith("claude-sonnet-")
            else "claude-sonnet-4-6"
        ),
        "ai_monthly_limit": max(1, min(1000, int(values.get("ai_monthly_limit", 100)))),
    }
