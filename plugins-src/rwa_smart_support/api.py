"""HTTP-слой плагина.

Пути и формы ответов совпадают с тем, что ожидает открытый UI панели
(``web/frontend/src/plugins/smart-support/api.ts`` и ``types.ts``) — роутер
монтируется ядром на ``/api/v2/plugins/smart_support``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from rwa_incident_hub import active_for_nodes

from . import actions as actions_mod
from . import ai, data, rules, settings as settings_mod, store
from .schemas import (
    ActionExecuteIn,
    ActionExecuteOut,
    ActionListResponse,
    AIProviderIn,
    AIProviderOut,
    AISettingsIn,
    AISettingsOut,
    AIStatusResponse,
    FeedbackIn,
    FeedbackOut,
    ReportResponse,
    SearchResponse,
    SessionListResponse,
    ThresholdSettings,
)


def build_router(ctx) -> APIRouter:
    from web.backend.core.plugin_api import auth_deps

    AdminUser, require_permission = auth_deps()
    router = APIRouter()

    can_view = require_permission("smart_support", "view")
    can_execute = require_permission("smart_support", "execute")

    db = ctx.db
    log = ctx.logger

    # ── поиск ────────────────────────────────────────────────────

    @router.get("/search", response_model=SearchResponse)
    async def search(
        q: str = Query(..., min_length=1, max_length=128),
        limit: int = Query(20, ge=1, le=50),
        _: Any = Depends(can_view),
    ) -> SearchResponse:
        matched_by, hits = await data.search_users(db, q.strip(), limit)
        ctx.telemetry.count("search")
        return SearchResponse(query=q, matched_by=matched_by, total=len(hits), hits=hits)

    # ── отчёт ────────────────────────────────────────────────────

    @router.get("/report/{user_uuid}", response_model=ReportResponse)
    async def report(user_uuid: str, admin: Any = Depends(can_view)) -> ReportResponse:
        thresholds = await settings_mod.get_thresholds(ctx.settings)

        user = await data.user_section(db, user_uuid)
        if user is None:
            raise HTTPException(status_code=404, detail={"code": "user_not_found"})

        latest_versions = await settings_mod.get_client_versions(ctx.settings)
        history = await data.history_section(db, user_uuid)
        client = await data.client_section(db, user_uuid, latest_versions)
        nodes = await data.nodes_section(db, user_uuid)
        incidents = await active_for_nodes(db, [node["uuid"] for node in nodes])
        violations = await data.violations_section(db, user_uuid)
        correlations = await store.correlations_for_user(
            db, user_uuid, thresholds["correlation_max_age_minutes"]
        )

        hypotheses = rules.evaluate(
            user=user, history=history, client=client, nodes=nodes,
            correlations=correlations, violations=violations, thresholds=thresholds,
        )
        if incidents:
            incident = incidents[0]
            details = incident.get("details") or {}
            hypotheses.insert(0, {
                "rule_id": "active_infrastructure_incident",
                "title": incident["title"],
                "detail": (
                    f"Активный инфраструктурный инцидент; transport="
                    f"{incident.get('transport') or 'unknown'}, "
                    f"просадка={details.get('drop_percent', '—')}%."
                ),
                "severity": "high",
                "confidence": 0.98,
                "suggested_action": "wait_or_switch_node",
            })

        payload: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc),
            "user": user,
            "history_24h": history,
            "client": client,
            "nodes": nodes,
            "correlations": correlations,
            "violations_recent": violations,
            "incidents_active": incidents,
            "hypotheses": hypotheses,
            "ai_analysis": None,
            "session_id": None,
        }

        payload["ai_analysis"] = await _maybe_analyze(payload)
        payload["session_id"] = await store.log_action(
            db,
            admin_username=getattr(admin, "username", None),
            target_user_uuid=user_uuid,
            action_id="report",
            triggered_by_rule_id=None,
            ok=True,
            message="report_generated",
            params={
                "rule_ids": [item["rule_id"] for item in hypotheses],
                "incident_ids": [item["id"] for item in incidents],
                "ai_provider": (
                    payload["ai_analysis"].get("provider_used")
                    if payload["ai_analysis"] else None
                ),
            },
        )
        ctx.telemetry.count("report")
        return ReportResponse(**payload)

    @router.post("/feedback", response_model=FeedbackOut)
    async def feedback(
        body: FeedbackIn, admin: Any = Depends(can_execute)
    ) -> FeedbackOut:
        feedback_id = await store.save_feedback(
            db,
            session_id=body.session_id,
            target_user_uuid=body.user_uuid,
            rule_id=body.rule_id,
            verdict=body.verdict,
            comment=(body.comment or "").strip() or None,
            admin_username=getattr(admin, "username", None),
        )
        if not feedback_id:
            raise HTTPException(status_code=500, detail="feedback_not_saved")
        ctx.telemetry.count(f"feedback.{body.verdict}")
        return FeedbackOut(
            id=feedback_id,
            rule_id=body.rule_id,
            verdict=body.verdict,
            summary=await store.feedback_summary(db, body.rule_id),
        )

    async def _maybe_analyze(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """ИИ — необязательная надстройка: если он выключен, не настроен,
        исчерпал лимит или упал, отчёт всё равно уходит целиком."""
        if (payload.get("client") or {}).get("source_stale"):
            log.warning("ai.skipped_stale_data")
            return None
        if not await settings_mod.ai_enabled(ctx.settings):
            return None
        cfg = await settings_mod.get_ai_provider(ctx.settings)
        if not cfg.get("api_key"):
            return None

        limit = int(cfg.get("monthly_limit") or 0)
        if limit and await store.ai_usage(db) >= limit:
            log.info("ai.quota_exhausted", extra={"limit": limit})
            return None

        try:
            result = await ai.analyze(cfg, payload)
        except ai.AIError as exc:
            log.warning("ai.call_failed", extra={"reason": str(exc)})
            return None
        except Exception:
            log.exception("ai.call_crashed")
            return None

        await store.bump_ai_usage(db)
        ctx.telemetry.count("ai_analysis")
        return result if result.get("summary") else None

    # ── настройки ────────────────────────────────────────────────

    @router.get("/settings", response_model=ThresholdSettings)
    async def get_settings(_: Any = Depends(can_view)) -> ThresholdSettings:
        return ThresholdSettings(**await settings_mod.get_thresholds(ctx.settings))

    @router.put("/settings", response_model=ThresholdSettings)
    async def put_settings(
        patch: ThresholdSettings, _: Any = Depends(can_execute)
    ) -> ThresholdSettings:
        resolved = await settings_mod.patch_thresholds(
            ctx.settings, patch.model_dump(exclude_none=True)
        )
        return ThresholdSettings(**resolved)

    @router.get("/ai-settings", response_model=AISettingsOut)
    async def get_ai_settings(_: Any = Depends(can_view)) -> AISettingsOut:
        return AISettingsOut(enabled=await settings_mod.ai_enabled(ctx.settings))

    @router.put("/ai-settings", response_model=AISettingsOut)
    async def put_ai_settings(
        patch: AISettingsIn, _: Any = Depends(can_execute)
    ) -> AISettingsOut:
        if patch.enabled is None:
            return AISettingsOut(enabled=await settings_mod.ai_enabled(ctx.settings))
        return AISettingsOut(
            enabled=await settings_mod.set_ai_enabled(ctx.settings, patch.enabled)
        )

    @router.get("/ai-status", response_model=AIStatusResponse)
    async def ai_status(_: Any = Depends(can_view)) -> AIStatusResponse:
        cfg = await settings_mod.get_ai_provider(ctx.settings)
        limit = int(cfg.get("monthly_limit") or 0)
        quota = None
        if limit:
            quota = {"period_limit": limit, "used": await store.ai_usage(db), "topup_left": 0}
        # Плагин свой и бесплатный: подписки нет, но UI ждёт одно из четырёх
        # состояний — «active», когда провайдер настроен и работать есть чем.
        return AIStatusResponse(
            enabled=await settings_mod.ai_enabled(ctx.settings),
            subscription_state="active" if cfg.get("api_key") else "missing",
            quota=quota,
        )

    @router.get("/ai-provider", response_model=AIProviderOut)
    async def get_ai_provider(_: Any = Depends(can_view)) -> AIProviderOut:
        cfg = await settings_mod.get_ai_provider(ctx.settings)
        return AIProviderOut(**settings_mod.redact(cfg))

    @router.put("/ai-provider", response_model=AIProviderOut)
    async def put_ai_provider(
        patch: AIProviderIn, _: Any = Depends(can_execute)
    ) -> AIProviderOut:
        cfg = await settings_mod.patch_ai_provider(
            ctx.settings, patch.model_dump(exclude_none=True)
        )
        return AIProviderOut(**settings_mod.redact(cfg))

    # ── действия ─────────────────────────────────────────────────

    @router.get("/actions", response_model=ActionListResponse)
    async def list_actions(_: Any = Depends(can_view)) -> ActionListResponse:
        return ActionListResponse(actions=actions_mod.CATALOG)

    @router.post("/actions/{action_id}/execute", response_model=ActionExecuteOut)
    async def execute_action(
        action_id: str,
        body: ActionExecuteIn,
        admin: Any = Depends(can_execute),
    ) -> ActionExecuteOut:
        result = await actions_mod.execute(
            db, action_id=action_id, user_uuid=body.user_uuid, params=body.params
        )
        await store.log_action(
            db,
            admin_username=getattr(admin, "username", None),
            target_user_uuid=body.user_uuid,
            action_id=action_id,
            triggered_by_rule_id=body.triggered_by_rule_id,
            ok=result["ok"],
            message=result["message"],
            params=body.params,
        )
        ctx.events.emit("action_executed", {
            "action_id": action_id,
            "user_uuid": body.user_uuid,
            "admin": getattr(admin, "username", None),
            "ok": result["ok"],
        })
        ctx.telemetry.count(f"action.{action_id}")

        if not result["ok"]:
            # 400, чтобы фронт показал текст ошибки в тосте, а не «успех».
            raise HTTPException(status_code=400, detail={"message": result["message"]})
        return ActionExecuteOut(**result)

    # ── журнал ───────────────────────────────────────────────────

    @router.get("/sessions/user/{user_uuid}", response_model=SessionListResponse)
    async def sessions_for_user(
        user_uuid: str,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        _: Any = Depends(can_view),
    ) -> SessionListResponse:
        items, total = await store.sessions_for_user(db, user_uuid, limit, offset)
        return SessionListResponse(items=items, total=total)

    @router.get("/sessions/recent", response_model=SessionListResponse)
    async def sessions_recent(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        action_id: Optional[str] = None,
        admin_username: Optional[str] = None,
        _: Any = Depends(can_view),
    ) -> SessionListResponse:
        items, total = await store.sessions_recent(
            db, limit=limit, offset=offset,
            action_id=action_id, admin_username=admin_username,
        )
        return SessionListResponse(items=items, total=total)

    return router
