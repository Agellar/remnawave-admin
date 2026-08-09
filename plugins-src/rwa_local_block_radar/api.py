"""API contract consumed by the built-in Block Radar UI."""
from __future__ import annotations

import zlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from . import settings as settings_mod, store

RBAC_RESOURCES = {"block_radar": ["view", "settings"]}


def _local_id(value: str | None) -> int:
    """Stable negative id; the UI hides the AS prefix for local ids."""
    return -int(zlib.crc32((value or "local").encode("utf-8")) or 1)


def _alert(row) -> dict:
    host = row["provider_name"] or row["node_name"]
    offline = not bool(row["node_alive"])
    return {
        "id": int(row["id"]),
        "kind": "hoster_outage" if offline else "block",
        "scope": "hoster",
        "op_asn": 0,
        "op_org": None,
        "host_asn": _local_id(host),
        "host_org": host,
        "transport": row["transport"] or "mixed",
        "since": row["since"].isoformat(),
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "panels": 1,
        "online": int(row["online"]),
        "baseline": round(float(row["baseline_online"]), 1),
        "outage_summary": "Локальная корреляция одной панели",
        "affected": {
            "nodes": [row["node_name"]],
            "online_now": int(row["online"]),
            "lost": offline,
        },
    }


def build_router(ctx, state: dict) -> APIRouter:
    from web.backend.core.plugin_api import auth_deps

    _, require_permission = auth_deps()
    can_view = require_permission("block_radar", "view")
    can_settings = require_permission("block_radar", "settings")
    router = APIRouter()

    @router.get("/status")
    async def status(_: Any = Depends(can_view)) -> dict:
        rows = await ctx.db.fetch(
            "SELECT * FROM local_block_radar_alerts WHERE resolved_at IS NULL ORDER BY since DESC"
        )
        dips = [
            {
                "node_uuid": str(row["node_uuid"]),
                "node_name": row["node_name"],
                "since": row["since"].isoformat(),
                "online": int(row["online"]),
                "baseline_online": round(float(row["baseline_online"]), 1),
                "share": float(row["share"]),
                "baseline_share": float(row["baseline_share"]),
                "node_alive": bool(row["node_alive"]),
            }
            for row in rows
        ]
        return {
            "last_tick": state.get("last_tick"),
            "open_alerts": len(rows),
            "license_usable": True,
            "open_dips": dips,
            "license_state": "not_required",
            "license_tier": "local",
            "license_paid_until": None,
        }

    @router.get("/alerts")
    async def alerts(
        active: bool | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: Any = Depends(can_view),
    ) -> dict:
        where = ""
        if active is True:
            where = "WHERE resolved_at IS NULL"
        elif active is False:
            where = "WHERE resolved_at IS NOT NULL"
        total = int(await ctx.db.fetchval(f"SELECT COUNT(*) FROM local_block_radar_alerts {where}") or 0)
        rows = await ctx.db.fetch(
            f"SELECT * FROM local_block_radar_alerts {where} ORDER BY since DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        return {"items": [_alert(row) for row in rows], "total": total}

    @router.get("/settings")
    async def get_settings(_: Any = Depends(can_view)) -> dict:
        return await settings_mod.get(ctx.settings)

    @router.put("/settings")
    async def put_settings(
        body: dict[str, Any] = Body(...), _: Any = Depends(can_settings)
    ) -> dict:
        return await settings_mod.patch(ctx.settings, body)

    @router.get("/overview")
    async def overview(_: Any = Depends(can_view)) -> dict:
        cfg = await settings_mod.get(ctx.settings)
        latest = await ctx.db.fetch(
            """SELECT DISTINCT ON (node_uuid) * FROM local_block_radar_samples
               ORDER BY node_uuid, sampled_at DESC"""
        )
        sites = []
        measured = 0
        providers: set[str] = set()
        for row in latest:
            provider = row["provider_name"] or row["node_name"]
            providers.add(provider)
            base = await store.baseline(ctx.db, str(row["node_uuid"]), int(cfg["dip_history_days"]))
            ready = bool(base and int(base["samples"] or 0) >= 60)
            measured += int(ready)
            sites.append({
                "host_asn": _local_id(provider),
                "host_org": provider,
                "transport": row["transport"] or "mixed",
                "online": int(row["online"]),
                "baseline": round(float(base["online"]), 1) if ready else None,
                "panels": 1,
            })
        incidents = int(await ctx.db.fetchval(
            "SELECT COUNT(*) FROM local_block_radar_alerts WHERE since >= NOW()-INTERVAL '30 days'"
        ) or 0)
        top = await ctx.db.fetch(
            """SELECT COALESCE(provider_name,node_name) AS provider, COUNT(*)::int AS incidents,
                      MAX(since) AS last_at
               FROM local_block_radar_alerts WHERE since >= NOW()-INTERVAL '30 days'
               GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
        )
        return {
            "links": {"total": len(latest), "measured": measured, "armed": measured},
            "operators": 0,
            "hosters": len(providers),
            "sites": sites,
            "network": {"panels": 1, "hosters": len(providers), "operators": 0},
            "pulse": {
                "days": 30, "incidents": incidents, "hosters": len(top),
                "blocks": incidents, "outages": 0,
                "hosters_top": [
                    {
                        "host_asn": _local_id(row["provider"]), "host_org": row["provider"],
                        "incidents": int(row["incidents"]), "blocks": int(row["incidents"]),
                        "outages": 0, "last_at": row["last_at"].isoformat(), "is_mine": True,
                    }
                    for row in top
                ],
            },
        }

    @router.get("/hosters")
    async def hosters(_: Any = Depends(can_view)) -> dict:
        rows = await ctx.db.fetch(
            """SELECT COALESCE(provider_name,node_name) AS provider,
                      COUNT(*)::int AS incidents,
                      COUNT(*) FILTER (WHERE resolved_at IS NOT NULL)::int AS resolved,
                      percentile_cont(0.5) WITHIN GROUP
                        (ORDER BY EXTRACT(EPOCH FROM (resolved_at-since))/60)
                        FILTER (WHERE resolved_at IS NOT NULL) AS recovery
               FROM local_block_radar_alerts GROUP BY 1 ORDER BY incidents DESC"""
        )
        first = await ctx.db.fetchval("SELECT MIN(sampled_at) FROM local_block_radar_samples")
        hours = max(0.0, (datetime.now(timezone.utc) - first).total_seconds() / 3600) if first else 0.0
        return {
            "window_days": 30,
            "min_panels": 1,
            "pending": 0,
            "locked": False,
            "hosters": [
                {
                    "asn": _local_id(row["provider"]), "org": row["provider"], "mine": True,
                    "panels": 1, "observed_hours": round(hours, 1), "operators": 0,
                    "outages": 0, "blocks": int(row["incidents"]), "blocked_operators": 0,
                    "outages_per_month": 0.0, "blocks_per_month": float(row["incidents"]),
                    "median_recovery_minutes": round(float(row["recovery"]), 1) if row["recovery"] else None,
                }
                for row in rows
            ],
        }

    return router
