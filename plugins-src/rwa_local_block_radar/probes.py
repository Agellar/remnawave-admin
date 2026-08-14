"""Continuous reachability checks from Globalping and trusted panel nodes."""
from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import time
from typing import Any

import httpx
from rwa_incident_hub import resolve as resolve_incident, upsert as upsert_incident

from . import store

GLOBALPING_API = "https://api.globalping.io/v1"
MAJOR_RU_ASNS = (8359, 12389, 41786)  # MTS, Rostelecom, ER-Telecom
FAILURE_STATES = {"regional_suspect", "endpoint_down"}
_probe_cache: dict[str, Any] = {"expires": 0.0, "items": []}


def _public_ip(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return str(address) if address.is_global else None


async def active_targets(db) -> list[dict]:
    rows = await db.fetch(
        """SELECT uuid::text AS uuid, COALESCE(NULLIF(remark,''),'Endpoint') AS name,
                  address, port, COALESCE(raw_data::jsonb->'nodes','[]'::jsonb) AS nodes
             FROM hosts WHERE NOT COALESCE(is_disabled,false)
             ORDER BY view_position, remark, uuid"""
    )
    targets = []
    for raw in rows:
        address = _public_ip(str(raw["address"] or ""))
        port = int(raw["port"] or 0)
        if not address or not 1 <= port <= 65535:
            continue
        linked = raw["nodes"] if isinstance(raw["nodes"], list) else []
        linked_ids = set()
        for item in linked:
            value = item.get("uuid") if isinstance(item, dict) else item
            if value:
                linked_ids.add(str(value))
        targets.append({
            "uuid": str(raw["uuid"]), "name": str(raw["name"]),
            "address": address, "port": port, "linked_nodes": linked_ids,
        })
    return targets


async def _available_probes(client: httpx.AsyncClient) -> list[dict]:
    now = time.monotonic()
    if now < float(_probe_cache["expires"]):
        return list(_probe_cache["items"])
    response = await client.get(f"{GLOBALPING_API}/probes")
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list):
        raise RuntimeError("globalping_invalid_probes")
    _probe_cache.update(expires=now + 3600, items=items)
    return items


def _locations(probes: list[dict]) -> list[dict]:
    live_ru_eye = {
        int(item.get("location", {}).get("asn") or 0)
        for item in probes
        if item.get("location", {}).get("country") == "RU"
        and "eyeball-network" in (item.get("tags") or [])
    }
    locations = [
        {"country": "RU", "asn": asn, "tags": ["eyeball-network"], "limit": 1}
        for asn in MAJOR_RU_ASNS if asn in live_ru_eye
    ]
    locations.extend([
        {"country": "RU", "tags": ["eyeball-network"], "limit": 1},
        {"country": "RU", "tags": ["datacenter-network"], "limit": 1},
        {"country": "DE", "tags": ["datacenter-network"], "limit": 1},
    ])
    return locations


def _globalping_result(item: dict) -> dict:
    probe = item.get("probe") or {}
    result = item.get("result") or {}
    tags = probe.get("tags") or []
    status = str(result.get("status") or "unknown")
    stats = result.get("stats") or {}
    success = status == "finished" and int(stats.get("rcv") or 0) > 0
    latency = stats.get("avg")
    network = str(probe.get("network") or "Globalping")[:160]
    asn = int(probe.get("asn") or 0) or None
    return {
        "source": "globalping",
        "vantage_label": f"{network} · AS{asn}" if asn else network,
        "country": probe.get("country"),
        "asn": asn,
        "network": network,
        "tags": tags,
        "success": success,
        "latency_ms": round(float(latency), 1) if latency is not None else None,
        "error_code": None if success else status,
    }


async def globalping_check(target: dict, timeout: int) -> tuple[list[dict], str | None, str | None]:
    token = os.getenv("GLOBALPING_API_TOKEN", "").strip()
    if not token:
        return [], None, "globalping_token_missing"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "Remnawave-Block-Radar/0.5"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            probes = await _available_probes(client)
            response = await client.post(
                f"{GLOBALPING_API}/measurements",
                json={
                    "type": "ping", "target": target["address"],
                    "locations": _locations(probes), "timeout": min(20, timeout + 4),
                    "measurementOptions": {
                        "packets": 3, "protocol": "TCP", "port": target["port"],
                    },
                },
            )
            response.raise_for_status()
            measurement_id = str(response.json()["id"])
            deadline = time.monotonic() + min(30, timeout + 12)
            payload = None
            while time.monotonic() < deadline:
                await asyncio.sleep(1.1)
                current = await client.get(f"{GLOBALPING_API}/measurements/{measurement_id}")
                current.raise_for_status()
                payload = current.json()
                if payload.get("status") != "in-progress":
                    break
            if not payload or payload.get("status") == "in-progress":
                return [], measurement_id, "globalping_timeout"
            return [
                _globalping_result(item) for item in payload.get("results", [])
                if isinstance(item, dict)
            ], measurement_id, None
    except httpx.HTTPStatusError as exc:
        return [], None, f"globalping_http_{exc.response.status_code}"
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError):
        return [], None, "globalping_unavailable"


def summarize(results: list[dict], previous: dict | None, cfg: dict, error_code: str | None) -> dict:
    gp = [row for row in results if row["source"] == "globalping"]
    ru = [
        row for row in gp if row.get("country") == "RU"
        and "eyeball-network" in (row.get("tags") or [])
    ]
    controls = [row for row in gp if row not in ru]
    ru_success = sum(bool(row["success"]) for row in ru)
    control_success = sum(bool(row["success"]) for row in controls)
    enough_ru = len(ru) >= int(cfg["probe_min_ru_results"])
    ru_ok = enough_ru and ru_success >= math.ceil(len(ru) / 2)

    if ru_ok:
        state = "healthy"
    elif not enough_ru:
        state = "insufficient"
    elif control_success > 0:
        state = "regional_suspect"
    else:
        state = "endpoint_down"

    previous_failures = int(previous.get("consecutive_failures") or 0) if previous else 0
    consecutive = previous_failures + 1 if state in FAILURE_STATES else 0
    was_open = bool(previous.get("incident_open")) if previous else False
    return {
        "state": state, "ru_success": ru_success, "ru_total": len(ru),
        "control_success": control_success, "control_total": len(controls),
        # Kept as zeroes for the existing storage schema and historical rows.
        "node_success": 0, "node_total": 0,
        "consecutive_failures": consecutive,
        "incident_open": (
            consecutive >= int(cfg["probe_confirm_cycles"])
            or (was_open and state != "healthy")
        ),
        "error_code": error_code,
    }


async def run_cycle(ctx, state: dict, cfg: dict) -> dict:
    targets = await active_targets(ctx.db)
    if not targets:
        return {"ok": False, "error": "no_public_targets", "targets": 0}
    cursor = int(state.get("probe_cursor", 0)) % len(targets)
    target = targets[cursor]
    state["probe_cursor"] = (cursor + 1) % len(targets)

    gp_results: list[dict] = []
    measurement_id = error_code = None
    if cfg["globalping_enabled"]:
        gp_results, measurement_id, error_code = await globalping_check(
            target, int(cfg["probe_timeout_seconds"])
        )
    results = gp_results
    previous = await store.previous_probe_cycle(ctx.db, target["uuid"])
    summary = summarize(results, previous, cfg, error_code)
    summary["measurement_id"] = measurement_id
    await store.save_probe_cycle(
        ctx.db, target=target, summary=summary, results=results,
    )

    incident_key = f"probe:{target['uuid']}"
    confirmed = summary["incident_open"]
    if confirmed and summary["state"] in FAILURE_STATES:
        kind = "regional_reachability" if summary["state"] == "regional_suspect" else "endpoint_unreachable"
        await upsert_incident(
            ctx.db, source_plugin="block_radar", incident_key=incident_key,
            kind=kind, severity="high" if kind == "endpoint_unreachable" else "medium",
            title=f"Недоступность endpoint: {target['name']}",
            details={
                "target_name": target["name"], "target_port": target["port"],
                "state": summary["state"], "ru_success": summary["ru_success"],
                "ru_total": summary["ru_total"],
                "control_success": summary["control_success"],
                "control_total": summary["control_total"],
                "confirmed_cycles": summary["consecutive_failures"],
            },
            node_uuid=next(iter(target["linked_nodes"]), None) if len(target["linked_nodes"]) == 1 else None,
            transport="tcp",
        )
    elif summary["state"] == "healthy":
        await resolve_incident(ctx.db, source_plugin="block_radar", incident_key=incident_key)

    return {
        "ok": True, "target_name": target["name"], "state": summary["state"],
        "targets": len(targets), "ru": f"{summary['ru_success']}/{summary['ru_total']}",
        "controls": f"{summary['control_success']}/{summary['control_total']}",
        "incident_open": confirmed, "error": error_code,
    }
