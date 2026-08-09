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
    }
