"""Read-only QCode account/key usage without exposing credentials."""
from __future__ import annotations

import os
from typing import Any

import httpx


BASE_URL = "https://qcode.cc/api/v1/openapi"


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int = 160) -> str | None:
    text = str(value or "").strip()
    return text[:limit] or None


def _account(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "active_api_keys": int(_number(source.get("active_api_keys")) or 0),
        "total_api_keys": int(_number(source.get("total_api_keys")) or 0),
        "today_cost_all_keys": _number(source.get("today_cost_all_keys")),
        "formatted_today_cost": _text(source.get("formatted_today_cost"), 40),
        "has_any_errors": bool(source.get("has_any_errors")),
        "last_updated": _text(source.get("last_updated"), 80),
    }


def _key(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "name": _text(source.get("name"), 120),
        "is_active": bool(source.get("is_active")),
        "expires_at": _text(source.get("expires_at"), 80),
        "expires_at_display": _text(source.get("expires_at_display"), 80),
        "current_daily_cost": _number(source.get("current_daily_cost")),
        "formatted_current_cost": _text(source.get("formatted_current_cost"), 40),
        "current_requests": int(_number(source.get("current_requests")) or 0),
        "current_tokens": int(_number(source.get("current_tokens")) or 0),
        "daily_cost_limit": _number(source.get("daily_cost_limit")),
        "has_monthly_quota": bool(source.get("has_monthly_quota")),
        "monthly_cost_limit": _number(source.get("monthly_cost_limit")),
        "monthly_cost_used": _number(source.get("monthly_cost_used")),
        "monthly_cost_percentage": _number(source.get("monthly_cost_percentage")),
        "opus_weekly_cost": _number(source.get("opus_weekly_cost")),
        "opus_weekly_limit": _number(source.get("opus_weekly_limit")),
        "is_near_cost_limit": bool(source.get("is_near_cost_limit")),
        "is_near_opus_limit": bool(source.get("is_near_opus_limit")),
        "has_error": bool(source.get("has_error")),
        "error_code": _text(source.get("error_code"), 80),
    }


async def usage_status() -> dict[str, Any]:
    """Fetch and strictly allowlist read-only QCode usage metadata."""
    token = os.getenv("QCODE_OPENAPI_TOKEN", "").strip()
    if not token:
        return {
            "configured": False,
            "ok": False,
            "error": "not_configured",
            "account": None,
            "keys": [],
        }

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0), follow_redirects=True
        ) as client:
            me_response = await client.get(f"{BASE_URL}/me", headers=headers)
            keys_response = await client.get(f"{BASE_URL}/keys", headers=headers)
        if me_response.status_code >= 400 or keys_response.status_code >= 400:
            code = max(me_response.status_code, keys_response.status_code)
            return {
                "configured": True,
                "ok": False,
                "error": f"http_{code}",
                "account": None,
                "keys": [],
            }
        me_payload = me_response.json()
        keys_payload = keys_response.json()
        if not me_payload.get("ok") or not keys_payload.get("ok"):
            return {
                "configured": True,
                "ok": False,
                "error": "upstream_error",
                "account": None,
                "keys": [],
            }
        account_data = me_payload.get("data")
        keys_data = keys_payload.get("data")
        if not isinstance(account_data, dict) or not isinstance(keys_data, dict):
            raise ValueError("invalid_data")
        raw_keys = keys_data.get("keys")
        if not isinstance(raw_keys, list):
            raise ValueError("invalid_keys")
        return {
            "configured": True,
            "ok": True,
            "error": None,
            "account": _account(account_data),
            "keys": [_key(item) for item in raw_keys[:100]],
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "configured": True,
            "ok": False,
            "error": "unavailable",
            "account": None,
            "keys": [],
        }
