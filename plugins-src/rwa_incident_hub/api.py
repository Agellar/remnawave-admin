"""Operator-facing API for the shared incident center."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from . import freshness, store


RBAC_RESOURCES = {"incident_center": ["view", "manage"]}
WORKFLOW_STATUSES = {"new", "acknowledged", "investigating", "snoozed"}
REVIEW_LABELS = {"confirmed", "false_positive", "unclear"}


def build_router(ctx) -> APIRouter:
    from web.backend.core.plugin_api import auth_deps

    _, require_permission = auth_deps()
    router = APIRouter()
    can_view = require_permission("incident_center", "view")
    can_manage = require_permission("incident_center", "manage")

    @router.get("/incidents")
    async def incidents(
        active: bool | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: Any = Depends(can_view),
    ) -> dict:
        await store.ensure_schema(ctx.db)
        items, total = await store.list_incidents(
            ctx.db, active=active, limit=limit, offset=offset
        )
        return {"items": items, "total": total}

    @router.get("/quality")
    async def quality(_: Any = Depends(can_view)) -> dict:
        await store.ensure_schema(ctx.db)
        return {"items": await store.quality_summary(ctx.db)}

    @router.get("/freshness")
    async def data_freshness(_: Any = Depends(can_view)) -> dict:
        return await freshness.system_status(ctx.db)

    @router.get("/incidents/{incident_id}/events")
    async def incident_events(
        incident_id: int, _: Any = Depends(can_view)
    ) -> dict:
        await store.ensure_schema(ctx.db)
        return {"items": await store.events(ctx.db, incident_id)}

    @router.put("/incidents/{incident_id}/workflow")
    async def workflow(
        incident_id: int,
        body: dict[str, Any] = Body(...),
        admin: Any = Depends(can_manage),
    ) -> dict:
        await store.ensure_schema(ctx.db)
        status = str(body.get("status") or "")
        if status not in WORKFLOW_STATUSES:
            raise HTTPException(status_code=422, detail="invalid_workflow_status")
        snooze = int(body.get("snooze_minutes") or 0)
        if status == "snoozed" and not 5 <= snooze <= 7 * 24 * 60:
            raise HTTPException(status_code=422, detail="invalid_snooze_minutes")
        assigned_to = str(body.get("assigned_to") or "").strip()[:120] or None
        note = str(body.get("note") or "").strip()[:1000] or None
        result = await store.set_workflow(
            ctx.db,
            incident_id=incident_id,
            status=status,
            snooze_minutes=snooze or None,
            assigned_to=assigned_to,
            note=note,
            actor=getattr(admin, "username", None),
        )
        if result is None:
            raise HTTPException(status_code=404, detail="incident_not_found")
        ctx.telemetry.count(f"workflow.{status}")
        return result

    @router.post("/incidents/{incident_id}/review")
    async def review(
        incident_id: int,
        body: dict[str, Any] = Body(...),
        admin: Any = Depends(can_manage),
    ) -> dict:
        await store.ensure_schema(ctx.db)
        label = str(body.get("label") or "")
        if label not in REVIEW_LABELS:
            raise HTTPException(status_code=422, detail="invalid_review_label")
        note = str(body.get("note") or "").strip()[:1000] or None
        result = await store.review(
            ctx.db,
            incident_id=incident_id,
            label=label,
            note=note,
            actor=getattr(admin, "username", None),
        )
        if result is None:
            raise HTTPException(status_code=404, detail="incident_not_found")
        ctx.telemetry.count(f"review.{label}")
        return result

    return router
