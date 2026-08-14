"""API contract consumed by the built-in Block Radar UI."""
from __future__ import annotations

import json
import math
import os
import time
import zlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from . import ai, qcode, settings as settings_mod, store

RBAC_RESOURCES = {"block_radar": ["view", "settings"]}


def _probe_schedule(state: dict) -> dict:
    next_epoch = state.get("next_probe_at_epoch")
    return {
        "probe_running": bool(state.get("probe_running")),
        "last_probe_at": state.get("last_probe_at"),
        "next_probe_at": (
            datetime.fromtimestamp(float(next_epoch), tz=timezone.utc).isoformat()
            if next_epoch else None
        ),
        "probe_interval_seconds": int(state.get("probe_interval_seconds") or 60),
    }


def _local_id(value: str | None) -> int:
    """Stable negative id; the UI hides the AS prefix for local ids."""
    return -int(zlib.crc32((value or "local").encode("utf-8")) or 1)


def _alert(row) -> dict:
    host = row["provider_name"] or row["node_name"]
    offline = not bool(row["node_alive"])
    def json_list(value) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return [str(item) for item in (value or [])]

    analysis = None
    if "ai_id" in row and row["ai_id"] is not None:
        analysis = {
            "id": int(row["ai_id"]),
            "alert_id": int(row["id"]),
            "classification": row["ai_classification"],
            "confidence": float(row["ai_confidence"]),
            "summary": row["ai_summary"],
            "evidence": json_list(row["ai_evidence"]),
            "recommendations": json_list(row["ai_recommendations"]),
            "support_note": row["ai_support_note"],
            "provider": row["ai_provider"],
            "model": row["ai_model"],
            "created_at": row["ai_created_at"].isoformat(),
            "updated_at": row["ai_updated_at"].isoformat(),
        }
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
        "ai_analysis": analysis,
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
            "last_probe": state.get("last_probe"),
            "globalping_configured": bool(os.getenv("GLOBALPING_API_TOKEN", "").strip()),
            "open_alerts": len(rows),
            "license_usable": True,
            "open_dips": dips,
            "license_state": "not_required",
            "license_tier": "local",
            "license_paid_until": None,
            **_probe_schedule(state),
        }

    @router.get("/probes")
    async def probes(_: Any = Depends(can_view)) -> dict:
        rows = await store.probe_overview(ctx.db)
        items = []
        for row in rows:
            results = row["results"]
            if isinstance(results, str):
                try:
                    results = json.loads(results)
                except ValueError:
                    results = []
            results = [
                result for result in (results or [])
                if isinstance(result, dict) and result.get("source") == "globalping"
            ]
            items.append({
                "target_uuid": str(row["target_uuid"]),
                "target_name": row["target_name"],
                "target_port": int(row["target_port"]),
                "state": row["state"],
                "ru_success": int(row["ru_success"]),
                "ru_total": int(row["ru_total"]),
                "control_success": int(row["control_success"]),
                "control_total": int(row["control_total"]),
                "consecutive_failures": int(row["consecutive_failures"]),
                "incident_open": bool(row["incident_open"]),
                "sampled_at": row["sampled_at"].isoformat(),
                "error_code": row["error_code"],
                "results": results,
            })
        cfg = await settings_mod.get(ctx.settings)
        return {
            "configured": bool(os.getenv("GLOBALPING_API_TOKEN", "").strip()),
            "enabled": bool(cfg["globalping_enabled"]),
            "last_cycle": state.get("last_probe"),
            "items": items,
            **_probe_schedule(state),
        }

    @router.post("/probes/run")
    async def run_probe_now(_: Any = Depends(can_settings)) -> dict:
        cfg = await settings_mod.get(ctx.settings)
        if not cfg["globalping_enabled"]:
            raise HTTPException(status_code=409, detail="globalping_disabled")
        if not os.getenv("GLOBALPING_API_TOKEN", "").strip():
            raise HTTPException(status_code=409, detail="globalping_token_missing")
        if state.get("probe_running"):
            raise HTTPException(status_code=409, detail="probe_already_running")
        retry_after = max(
            0,
            math.ceil(float(state.get("manual_cooldown_until") or 0) - time.time()),
        )
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={"code": "probe_cooldown", "retry_after": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
        runner = state.get("run_probe")
        if not callable(runner):
            raise HTTPException(status_code=503, detail="probe_runner_unavailable")
        try:
            result = await runner(manual=True)
        except RuntimeError as exc:
            code = str(exc)
            if code in {"probe_already_running", "globalping_disabled"}:
                raise HTTPException(status_code=409, detail=code) from exc
            raise
        return {"result": result, **_probe_schedule(state)}

    @router.get("/alerts")
    async def alerts(
        active: bool | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: Any = Depends(can_view),
    ) -> dict:
        where = ""
        if active is True:
            where = "WHERE a.resolved_at IS NULL"
        elif active is False:
            where = "WHERE a.resolved_at IS NOT NULL"
        total = int(await ctx.db.fetchval(
            f"SELECT COUNT(*) FROM local_block_radar_alerts a {where}"
        ) or 0)
        rows = await ctx.db.fetch(
            f"""SELECT a.*,
                       x.id AS ai_id, x.classification AS ai_classification,
                       x.confidence AS ai_confidence, x.summary AS ai_summary,
                       x.evidence AS ai_evidence,
                       x.recommendations AS ai_recommendations,
                       x.support_note AS ai_support_note, x.provider AS ai_provider,
                       x.model AS ai_model, x.created_at AS ai_created_at,
                       x.updated_at AS ai_updated_at
                  FROM local_block_radar_alerts a
             LEFT JOIN local_block_radar_ai_analyses x ON x.alert_id=a.id
                  {where} ORDER BY a.since DESC LIMIT $1 OFFSET $2""",
            limit, offset,
        )
        return {"items": [_alert(row) for row in rows], "total": total}

    @router.get("/ai/status")
    async def ai_status(_: Any = Depends(can_view)) -> dict:
        return await ai.provider_status(ctx.settings, ctx.db)

    @router.get("/ai/qcode-usage")
    async def qcode_usage(_: Any = Depends(can_settings)) -> dict:
        return await qcode.usage_status()

    @router.post("/alerts/{alert_id}/ai/analyze")
    async def analyze_alert(
        alert_id: int,
        body: dict[str, Any] | None = Body(default=None),
        _: Any = Depends(can_settings),
    ) -> dict:
        try:
            return await ai.analyze_alert(
                ctx, alert_id, force=bool((body or {}).get("force", False))
            )
        except ai.AIError as exc:
            detail = str(exc)
            if detail == "alert_not_found":
                raise HTTPException(status_code=404, detail=detail) from exc
            if detail in {"ai_disabled", "monthly_limit_reached"}:
                raise HTTPException(status_code=409, detail=detail) from exc
            raise HTTPException(status_code=503, detail=detail) from exc

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
