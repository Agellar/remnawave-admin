"""HTTP-слой плагина. Роутер монтируется ядром на
``/api/v2/plugins/retention_radar``.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import bedolaga, campaigns, data, settings as settings_mod, store
from .schemas import (
    CampaignHistoryResponse,
    CampaignPreviewIn,
    CampaignPreviewOut,
    CampaignSendIn,
    CampaignSendOut,
    OverviewResponse,
    SegmentCard,
    SegmentResponse,
    SegmentUser,
    ThresholdPatch,
    ThresholdSettings,
)

# Рассылка живым людям — отдельное право, а не часть «экспорта»:
# выгрузить список и разослать 200 сообщений это разные полномочия.
RBAC_RESOURCES = {"retention_radar": ["view", "export", "campaign"]}

CSV_COLUMNS = (
    "username",
    "telegram_id",
    "status",
    "expire_at",
    "last_online",
    "days_silent",
    "days_until_expire",
    "used_traffic_bytes",
    "tag",
    "uuid",
)


def build_router(ctx) -> APIRouter:
    from web.backend.core.plugin_api import auth_deps

    AdminUser, require_permission = auth_deps()
    router = APIRouter()

    can_view = require_permission("retention_radar", "view")
    can_export = require_permission("retention_radar", "export")
    can_campaign = require_permission("retention_radar", "campaign")

    db = ctx.db

    def _check_segment(key: str) -> str:
        if key not in data.SEGMENTS:
            raise HTTPException(status_code=404, detail="unknown_segment")
        return key

    # ── дашборд ──────────────────────────────────────────────────

    @router.get("/overview", response_model=OverviewResponse)
    async def overview(_: Any = Depends(can_view)) -> OverviewResponse:
        th = await settings_mod.get_thresholds(ctx.settings)
        counts = await data.counts(db, th)
        totals = await data.totals(db)
        trend_days = int(th["trend_days"])

        cards = []
        for key in data.SEGMENTS:
            previous = await store.previous_value(db, key)
            cards.append(
                SegmentCard(
                    key=key,
                    users_count=counts[key],
                    delta=None if previous is None else counts[key] - previous,
                    trend=await store.trend(db, key, trend_days),
                )
            )

        ctx.telemetry.count("overview")
        return OverviewResponse(
            generated_at=datetime.now(timezone.utc),
            total_users=totals["total"],
            active_users=totals["active"],
            segments=cards,
            attention=[SegmentUser(**u) for u in await data.attention(db, th)],
            history_since=await store.first_day(db),
        )

    # ── сегмент ──────────────────────────────────────────────────

    @router.get("/segment/{key}", response_model=SegmentResponse)
    async def segment(
        key: str,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        _: Any = Depends(can_view),
    ) -> SegmentResponse:
        _check_segment(key)
        th = await settings_mod.get_thresholds(ctx.settings)
        users = await data.list_segment(db, key, th, limit, offset)
        return SegmentResponse(
            key=key,
            total=await data.count_segment(db, key, th),
            limit=limit,
            offset=offset,
            users=[SegmentUser(**u) for u in users],
        )

    @router.get("/segment/{key}/export")
    async def export_segment(key: str, _: Any = Depends(can_export)) -> StreamingResponse:
        """Весь сегмент одним CSV — чтобы отдать его в рассылку.

        Потолок в 5000 строк: это выгрузка для кампании, а не дамп базы,
        и держать в памяти воркера больше незачем.
        """
        _check_segment(key)
        th = await settings_mod.get_thresholds(ctx.settings)
        users = await data.list_segment(db, key, th, limit=5000, offset=0)

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for user in users:
            writer.writerow(
                [
                    "" if user.get(column) is None else user.get(column)
                    for column in CSV_COLUMNS
                ]
            )
        buffer.seek(0)

        ctx.telemetry.count("export")
        ctx.logger.info(
            "retention_radar.exported", extra={"segment": key, "rows": len(users)}
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="retention-{key}-{stamp}.csv"'
            },
        )

    # ── кампании ─────────────────────────────────────────────────

    @router.post("/campaign/preview", response_model=CampaignPreviewOut)
    async def campaign_preview(
        body: CampaignPreviewIn, _: Any = Depends(can_campaign)
    ) -> CampaignPreviewOut:
        _check_segment(body.segment)
        th = await settings_mod.get_thresholds(ctx.settings)
        result = await campaigns.preview(db, ctx, body.segment, th, body.message_text)
        result["sample"] = [SegmentUser(**u) for u in result["sample"]]
        return CampaignPreviewOut(**result)

    @router.post("/campaign/send", response_model=CampaignSendOut)
    async def campaign_send(
        body: CampaignSendIn, admin: Any = Depends(can_campaign)
    ) -> CampaignSendOut:
        _check_segment(body.segment)
        th = await settings_mod.get_thresholds(ctx.settings)
        cfg = settings_mod.get_bedolaga_config()
        if not body.dry_run and not cfg.get("token"):
            raise HTTPException(status_code=503, detail="bedolaga_not_configured")

        try:
            result = await campaigns.send(
                db,
                ctx,
                segment=body.segment,
                message_text=body.message_text.strip(),
                token=body.confirm_token,
                dry_run=body.dry_run,
                admin_username=getattr(admin, "username", None),
                th=th,
                bedolaga_cfg=cfg,
            )
        except ValueError as exc:
            # stale_confirmation / empty_audience — это не сбой сервера,
            # а «предпросмотр устарел, посмотрите заново».
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except bedolaga.BedolagaError as exc:
            raise HTTPException(status_code=502, detail=f"bedolaga:{exc}") from exc
        return CampaignSendOut(**result)

    @router.get("/campaigns", response_model=CampaignHistoryResponse)
    async def campaign_history(
        limit: int = Query(20, ge=1, le=100), _: Any = Depends(can_view)
    ) -> CampaignHistoryResponse:
        return CampaignHistoryResponse(items=await store.list_campaigns(db, limit))

    # ── настройки ────────────────────────────────────────────────

    @router.get("/settings", response_model=ThresholdSettings)
    async def get_settings(_: Any = Depends(can_view)) -> ThresholdSettings:
        return ThresholdSettings(**await settings_mod.get_thresholds(ctx.settings))

    @router.put("/settings", response_model=ThresholdSettings)
    async def put_settings(
        body: ThresholdPatch, _: Any = Depends(can_export)
    ) -> ThresholdSettings:
        values = await settings_mod.patch_thresholds(ctx.settings, body.values)
        return ThresholdSettings(**values)

    return router
