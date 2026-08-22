"""Bounded external outage signal for the client's most-used public ASN.

Only the ASN number is sent to IODA. Failures are deliberately silent for the
report: an unavailable third-party service is not evidence that either the ISP
or our infrastructure is healthy.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

IODA_ALERTS_URL = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"
REQUEST_TIMEOUT_SECONDS = 5.0
CACHE_TTL_SECONDS = 3 * 60 * 60
CACHE_MAX_ENTRIES = 256

# ASN -> (monotonic expiry, parsed verdict). Negative answers are cached too.
_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}


def _asn_number(value: Any) -> Optional[int]:
    match = re.fullmatch(r"(?:AS)?([1-9][0-9]{0,9})", str(value or "").strip(), re.I)
    if not match:
        return None
    number = int(match.group(1))
    return number if number <= 4_294_967_295 else None


def _primary_network(history: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Choose the most frequently observed routable ASN, newest on ties."""
    rows = history.get("timeline") if isinstance(history, dict) else None
    if not isinstance(rows, list):
        return None
    parsed = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        asn = _asn_number(row.get("asn"))
        if asn is not None:
            parsed.append((asn, index, row))
    if not parsed:
        return None
    counts = Counter(asn for asn, _, _ in parsed)
    chosen = min(parsed, key=lambda item: (-counts[item[0]], item[1]))
    return {
        "asn": chosen[0],
        "label": chosen[2].get("asn_org") or f"AS{chosen[0]}",
    }


def _parse_alerts(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    active = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("level") or "").lower() in {"warning", "critical"}
    ]
    if not active:
        return {"has_outage": False}
    severity = (
        "critical"
        if any(str(row.get("level") or "").lower() == "critical" for row in active)
        else "warning"
    )
    methods = sorted(
        {
            str(row.get("datasource")).strip()
            for row in active
            if str(row.get("datasource") or "").strip()
        }
    )[:5]
    return {"has_outage": True, "severity": severity, "methods": methods}


async def _probe(asn: int) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    params = {
        "from": int((now - timedelta(hours=24)).timestamp()),
        "until": int(now.timestamp()),
        "entityType": "asn",
        "entityCode": str(asn),
    }
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(IODA_ALERTS_URL, params=params)
        if response.status_code != 200:
            logger.warning("smart_support.ioda_http", extra={"status": response.status_code})
            return None
        return _parse_alerts(response.json())
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
        logger.warning("smart_support.ioda_failed", exc_info=True)
        return None


def _cache_get(asn: int) -> Optional[Dict[str, Any]]:
    item = _cache.get(asn)
    if not item:
        return None
    expires_at, verdict = item
    if expires_at <= time.monotonic():
        _cache.pop(asn, None)
        return None
    return verdict


def _cache_put(asn: int, verdict: Dict[str, Any]) -> None:
    if len(_cache) >= CACHE_MAX_ENTRIES and asn not in _cache:
        oldest = min(_cache, key=lambda key: _cache[key][0])
        _cache.pop(oldest, None)
    _cache[asn] = (time.monotonic() + CACHE_TTL_SECONDS, verdict)


async def signal_for(
    history: Dict[str, Any], *, enabled: bool
) -> Optional[Dict[str, Any]]:
    if not enabled:
        return None
    network = _primary_network(history)
    if not network:
        return None
    asn = int(network["asn"])
    verdict = _cache_get(asn)
    if verdict is None:
        verdict = await _probe(asn)
        if verdict is None:
            return None
        _cache_put(asn, verdict)
    if not verdict.get("has_outage"):
        return None
    return {
        "asn": f"AS{asn}",
        "org": network["label"],
        "severity": verdict.get("severity") or "warning",
        "methods": list(verdict.get("methods") or []),
        "source": "ioda",
    }


def clear_cache() -> None:
    """Test and operational hook; no persistent user data is stored."""
    _cache.clear()
