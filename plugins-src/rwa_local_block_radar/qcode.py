"""Read-only QCode account/key usage without exposing credentials."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx


BASE_URL = "https://qcode.cc/api/v1/openapi"
CACHE_TTL_SECONDS = 60
_cache: dict[str, Any] | None = None
_cache_until = 0.0
_cache_token_hash = ""
_cache_lock = asyncio.Lock()


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


def _expiry(value: Any) -> tuple[int | None, str]:
    text = _text(value, 80)
    if not text:
        return None, "unknown"
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return None, "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    days = int((parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() // 86400)
    if days < 0:
        return days, "expired"
    if days <= 3:
        return days, "critical"
    if days <= 14:
        return days, "warning"
    return days, "ok"


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
    expires_at = _text(source.get("expires_at"), 80)
    expires_display = _text(source.get("expires_at_display"), 80)
    expires_in_days, expiry_warning = _expiry(expires_at or expires_display)
    daily_cost = _number(source.get("current_daily_cost"))
    daily_limit = _number(source.get("daily_cost_limit"))
    near_limit = bool(source.get("is_near_cost_limit"))
    if daily_cost is not None and daily_limit and daily_limit > 0:
        near_limit = near_limit or float(daily_cost) / float(daily_limit) >= 0.8
    return {
        "name": _text(source.get("name"), 120),
        "is_active": bool(source.get("is_active")),
        "expires_at": expires_at,
        "expires_at_display": expires_display,
        "expires_in_days": expires_in_days,
        "expiry_warning": expiry_warning,
        "current_daily_cost": daily_cost,
        "formatted_current_cost": _text(source.get("formatted_current_cost"), 40),
        "current_requests": int(_number(source.get("current_requests")) or 0),
        "current_tokens": int(_number(source.get("current_tokens")) or 0),
        "daily_cost_limit": daily_limit,
        "has_monthly_quota": bool(source.get("has_monthly_quota")),
        "monthly_cost_limit": _number(source.get("monthly_cost_limit")),
        "monthly_cost_used": _number(source.get("monthly_cost_used")),
        "monthly_cost_percentage": _number(source.get("monthly_cost_percentage")),
        "opus_weekly_cost": _number(source.get("opus_weekly_cost")),
        "opus_weekly_limit": _number(source.get("opus_weekly_limit")),
        "is_near_cost_limit": near_limit,
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

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    if _cache is not None and _cache_token_hash == token_hash and now < _cache_until:
        return _cache

    async with _cache_lock:
        now = time.monotonic()
        if _cache is not None and _cache_token_hash == token_hash and now < _cache_until:
            return _cache
        return await _fetch(token, token_hash)


async def _fetch(token: str, token_hash: str) -> dict[str, Any]:
    global _cache, _cache_until, _cache_token_hash
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
        result = {
            "configured": True,
            "ok": True,
            "error": None,
            "account": _account(account_data),
            "keys": [_key(item) for item in raw_keys[:100]],
            "fetched_at": datetime.now(timezone.utc),
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
        }
        _cache = result
        _cache_until = time.monotonic() + CACHE_TTL_SECONDS
        _cache_token_hash = token_hash
        return result
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "configured": True,
            "ok": False,
            "error": "unavailable",
            "account": None,
            "keys": [],
        }


def _reset_cache() -> None:
    global _cache, _cache_until, _cache_token_hash
    _cache = None
    _cache_until = 0.0
    _cache_token_hash = ""
