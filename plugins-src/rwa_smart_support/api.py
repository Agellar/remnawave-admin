"""HTTP-слой плагина.

Пути и формы ответов совпадают с тем, что ожидает открытый UI панели
(``web/frontend/src/plugins/smart-support/api.ts`` и ``types.ts``) — роутер
монтируется ядром на ``/api/v2/plugins/smart_support``.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from rwa_incident_hub import active_for_nodes

from . import actions as actions_mod
from . import ai, data, outages, rules, settings as settings_mod, store
from .schemas import (
    ActionExecuteIn,
    ActionExecuteOut,
    ActionListResponse,
    AIProviderIn,
    AIProviderOut,
    AISettingsIn,
    AISettingsOut,
    AIStatusResponse,
    ClientReferenceResponse,
    ClientSubmissionCreated,
    ClientSubmissionIn,
    ClientSubmissionListResponse,
    ClientVoteIn,
    ClientVoteOut,
    FeedbackIn,
    FeedbackOut,
    ReportResponse,
    SearchResponse,
    SessionListResponse,
    ThresholdSettings,
)


def build_router(ctx) -> APIRouter:
    from web.backend.core.plugin_api import CloudError, auth_deps

    AdminUser, require_permission = auth_deps()
    router = APIRouter()

    can_view = require_permission("smart_support", "view")
    can_edit = require_permission("smart_support", "edit")

    async def can_execute(admin: AdminUser = Depends(can_view)):
        """Accept the new granular permission and the legacy execute grant."""
        if getattr(admin, "account_id", None) is None or getattr(admin, "role", None) == "superadmin":
            return admin
        if admin.has_permission("smart_support", "run_actions") or admin.has_permission(
            "smart_support", "execute"
        ):
            return admin
        raise HTTPException(
            status_code=403,
            detail="Permission denied: smart_support:run_actions",
        )

    db = ctx.db
    log = ctx.logger
    # Runtime-only provider health. Values contain a generic code, never an
    # upstream response body or API key, and disappear on process restart.
    provider_health: Dict[str, Dict[str, Any]] = {}

    async def visible_user_uuids(admin: Any) -> Optional[set[str]]:
        """Resolve panel user visibility and fail closed for scoped admins."""
        if getattr(admin, "account_id", None) is None or getattr(admin, "role", None) == "superadmin":
            return None
        try:
            from shared.rbac import get_visible_user_uuids

            visible = await get_visible_user_uuids(
                getattr(admin, "account_id", None),
                getattr(admin, "role", None),
            )
        except Exception:  # noqa: BLE001 -- authorization must fail closed
            log.warning("smart_support.user_scope_failed", exc_info=True)
            return set()
        if visible is None:
            return None
        return {str(item).strip().lower() for item in visible if str(item).strip()}

    async def require_visible_user(admin: Any, user_uuid: str) -> str:
        """Return a normalized visible UUID; hidden and invalid IDs are alike."""
        normalized = str(user_uuid or "").strip().lower()
        if not data.UUID_RE.fullmatch(normalized):
            raise HTTPException(status_code=404, detail={"code": "user_not_found"})
        visible = await visible_user_uuids(admin)
        if visible is not None and normalized not in visible:
            raise HTTPException(status_code=404, detail={"code": "user_not_found"})
        return normalized

    def provider_error_code(exc: Exception) -> str:
        raw = str(exc).strip().lower()
        match = re.match(r"^(http_\d{3}|[a-z0-9_]{1,64})", raw)
        return match.group(1) if match else "provider_error"

    def park_provider(provider: str, code: str) -> None:
        seconds = 300
        if code in ("http_401", "http_403", "api_key_missing"):
            seconds = 900
        elif code not in ("http_429", "http_500", "http_502", "http_503", "http_504"):
            seconds = 60
        provider_health[provider] = {
            "until": time.monotonic() + seconds,
            "last_error": code,
        }

    def provider_cooldown(provider: str) -> int:
        item = provider_health.get(provider) or {}
        return max(0, int(float(item.get("until") or 0) - time.monotonic()))

    # ── поиск ────────────────────────────────────────────────────

    @router.get("/search", response_model=SearchResponse)
    async def search(
        q: str = Query(..., min_length=1, max_length=128),
        limit: int = Query(20, ge=1, le=50),
        admin: Any = Depends(can_view),
    ) -> SearchResponse:
        visible = await visible_user_uuids(admin)
        matched_by, hits = await data.search_users(
            db, q.strip(), limit, visible_user_uuids=visible
        )
        ctx.telemetry.count("search")
        return SearchResponse(query=q, matched_by=matched_by, total=len(hits), hits=hits)

    # ── отчёт ────────────────────────────────────────────────────

    @router.get("/report/{user_uuid}", response_model=ReportResponse)
    async def report(user_uuid: str, admin: Any = Depends(can_view)) -> ReportResponse:
        user_uuid = await require_visible_user(admin, user_uuid)
        thresholds = await settings_mod.get_thresholds(ctx.settings)

        user = await data.user_section(db, user_uuid)
        if user is None:
            raise HTTPException(status_code=404, detail={"code": "user_not_found"})

        latest_versions = await settings_mod.get_client_versions(ctx.settings)
        history = await data.history_section(db, user_uuid)
        client = await data.client_section(db, user_uuid, latest_versions)
        nodes = await data.nodes_section(db, user_uuid)
        throttle = await data.throttle_section(db, user_uuid)
        # Local, verified Incident Center state is independent from the
        # optional external ISP outage lookup and must always remain visible.
        incidents = await active_for_nodes(db, [node["uuid"] for node in nodes])
        violations = await data.violations_section(db, user_uuid)
        correlations = await store.correlations_for_user(
            db,
            user_uuid,
            thresholds["correlation_max_age_minutes"],
            thresholds["correlation_history_minutes"],
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
            "throttle": throttle,
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
        user_uuid = await require_visible_user(admin, body.user_uuid)
        if body.session_id is not None:
            session_target = await db.fetchval(
                "SELECT target_user_uuid::text FROM smart_support_sessions WHERE id = $1",
                body.session_id,
            )
            if str(session_target or "").lower() != user_uuid:
                raise HTTPException(status_code=404, detail={"code": "session_not_found"})
        feedback_id = await store.save_feedback(
            db,
            session_id=body.session_id,
            target_user_uuid=user_uuid,
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
        ai_settings = await settings_mod.get_ai_settings(ctx.settings)
        providers = await settings_mod.get_ai_providers(ctx.settings)
        configured = [cfg for cfg in providers if cfg.get("api_key")]
        if not configured:
            return None

        limit = int(configured[0].get("monthly_limit") or 0)
        provider_outage = await outages.signal_for(
            payload.get("history_24h") or {},
            enabled=bool(ai_settings.get("outage_lookup_enabled")),
        )
        if provider_outage:
            # The external service receives only the ASN. Keep the returned
            # signal in the AI-only copy so it does not alter the public report
            # schema or expose new user data.
            payload = {**payload, "provider_outage": provider_outage}

        result: Optional[Dict[str, Any]] = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 25.0
        for cfg in configured:
            provider_name = str(cfg.get("provider") or "unknown")
            if provider_cooldown(provider_name) > 0:
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("ai.total_timeout")
                break
            # Reserve one unit for every outbound attempt, including failures:
            # provider failover must not bypass an operator-defined spend cap.
            if not await store.reserve_ai_usage(db, limit):
                log.info("ai.quota_exhausted", extra={"limit": limit})
                break
            try:
                candidate = await asyncio.wait_for(
                    ai.analyze(cfg, payload), timeout=min(12.0, remaining)
                )
            except asyncio.TimeoutError:
                park_provider(provider_name, "timeout")
                log.warning("ai.call_failed", extra={"provider": provider_name, "reason": "timeout"})
                continue
            except ai.AIError as exc:
                code = provider_error_code(exc)
                park_provider(provider_name, code)
                log.warning(
                    "ai.call_failed",
                    extra={"provider": provider_name, "reason": code},
                )
                continue
            except Exception:
                park_provider(provider_name, "internal_error")
                log.exception("ai.call_crashed", extra={"provider": provider_name})
                continue
            if candidate.get("summary"):
                provider_health.pop(provider_name, None)
                result = candidate
                break

        if result is None:
            return None

        ctx.telemetry.count("ai_analysis")
        return result

    # ── настройки ────────────────────────────────────────────────

    @router.get("/settings", response_model=ThresholdSettings)
    async def get_settings(_: Any = Depends(can_view)) -> ThresholdSettings:
        return ThresholdSettings(**await settings_mod.get_thresholds(ctx.settings))

    @router.put("/settings", response_model=ThresholdSettings)
    async def put_settings(
        patch: ThresholdSettings, _: Any = Depends(can_edit)
    ) -> ThresholdSettings:
        resolved = await settings_mod.patch_thresholds(
            ctx.settings, patch.model_dump(exclude_none=True)
        )
        return ThresholdSettings(**resolved)

    @router.get("/ai-settings", response_model=AISettingsOut)
    async def get_ai_settings(_: Any = Depends(can_view)) -> AISettingsOut:
        resolved = await settings_mod.get_ai_settings(ctx.settings)
        return AISettingsOut(**settings_mod.public_ai_settings(resolved))

    @router.put("/ai-settings", response_model=AISettingsOut)
    async def put_ai_settings(
        patch: AISettingsIn, _: Any = Depends(can_edit)
    ) -> AISettingsOut:
        resolved = await settings_mod.patch_ai_settings(
            ctx.settings, patch.model_dump(exclude_none=True)
        )
        provider_health.clear()
        return AISettingsOut(**settings_mod.public_ai_settings(resolved))

    @router.get("/ai-status", response_model=AIStatusResponse)
    async def ai_status(_: Any = Depends(can_view)) -> AIStatusResponse:
        providers = await settings_mod.get_ai_providers(ctx.settings)
        configured = [cfg for cfg in providers if cfg.get("api_key")]
        limit = int(configured[0].get("monthly_limit") or 0) if configured else 0
        quota = None
        if limit:
            quota = {"period_limit": limit, "used": await store.ai_usage(db), "topup_left": 0}
        # Плагин свой и бесплатный: подписки нет, но UI ждёт одно из четырёх
        # состояний — «active», когда провайдер настроен и работать есть чем.
        return AIStatusResponse(
            enabled=await settings_mod.ai_enabled(ctx.settings),
            configured=bool(configured),
            providers=[
                {
                    "provider": cfg["provider"],
                    "configured": bool(cfg.get("api_key")),
                    "available": bool(cfg.get("api_key"))
                    and provider_cooldown(cfg["provider"]) == 0,
                    "cooldown_seconds_remaining": provider_cooldown(cfg["provider"]),
                    "last_error": (provider_health.get(cfg["provider"]) or {}).get("last_error"),
                }
                for cfg in providers
            ],
            subscription_state="active" if configured else "missing",
            quota=quota,
        )

    @router.get("/ai-provider", response_model=AIProviderOut)
    async def get_ai_provider(_: Any = Depends(can_view)) -> AIProviderOut:
        cfg = await settings_mod.get_ai_provider(ctx.settings)
        return AIProviderOut(**settings_mod.redact(cfg))

    @router.put("/ai-provider", response_model=AIProviderOut)
    async def put_ai_provider(
        patch: AIProviderIn, admin: Any = Depends(can_edit)
    ) -> AIProviderOut:
        if (patch.base_url is not None or patch.proxy is not None) and not (
            getattr(admin, "account_id", None) is None
            or getattr(admin, "role", None) == "superadmin"
        ):
            raise HTTPException(
                status_code=403,
                detail="Provider transport overrides require superadmin",
            )
        cfg = await settings_mod.patch_ai_provider(
            ctx.settings, patch.model_dump(exclude_none=True)
        )
        provider_health.clear()
        return AIProviderOut(**settings_mod.redact(cfg))

    # ── общий справочник клиентских приложений ──────────────────

    def cloud_failure(exc: CloudError, *, status_code: int = 503) -> HTTPException:
        # Детали upstream могут содержать внутренний URL/ответ. В UI отдаём
        # стабильный код, а полную причину пишем только в серверный лог.
        log.warning("smart_support.clients.cloud_failed", extra={"code": exc.code})
        return HTTPException(
            status_code=status_code,
            detail={"code": exc.code or "cloud_unavailable"},
        )

    def require_cloud_method(name: str):
        method = getattr(ctx.cloud, name, None)
        if not callable(method):
            raise HTTPException(status_code=501, detail={"code": "panel_update_required"})
        return method

    @router.get("/clients", response_model=ClientReferenceResponse)
    async def clients(_: Any = Depends(can_view)) -> ClientReferenceResponse:
        try:
            payload = await require_cloud_method("client_apps")()
        except CloudError as exc:
            raise cloud_failure(exc) from exc
        return ClientReferenceResponse(**(payload or {}))

    @router.get("/clients/submissions", response_model=ClientSubmissionListResponse)
    async def client_submissions(
        status: Literal["open", "all"] = "open",
        _: Any = Depends(can_view),
    ) -> ClientSubmissionListResponse:
        try:
            payload = await require_cloud_method("client_submissions")(status)
        except CloudError as exc:
            raise cloud_failure(exc) from exc
        return ClientSubmissionListResponse(**(payload or {}))

    @router.post("/clients/submissions", response_model=ClientSubmissionCreated)
    async def submit_client_change(
        body: ClientSubmissionIn,
        _: Any = Depends(can_edit),
    ) -> ClientSubmissionCreated:
        payload = body.model_dump(exclude_none=True)
        if len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")) > 16_384:
            raise HTTPException(status_code=413, detail={"code": "payload_too_large"})
        try:
            result = await require_cloud_method("submit_client_change")(payload)
        except CloudError as exc:
            raise cloud_failure(exc, status_code=502) from exc
        ctx.telemetry.count("clients.submission")
        return ClientSubmissionCreated(**(result or {}))

    @router.post("/clients/submissions/{submission_id}/vote", response_model=ClientVoteOut)
    async def vote_client_submission(
        submission_id: int,
        body: ClientVoteIn,
        _: Any = Depends(can_edit),
    ) -> ClientVoteOut:
        if submission_id <= 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_submission_id"})
        try:
            result = await require_cloud_method("vote_client_submission")(submission_id, body.vote)
        except CloudError as exc:
            raise cloud_failure(exc, status_code=502) from exc
        ctx.telemetry.count("clients.vote")
        return ClientVoteOut(**(result or {}))

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
        user_uuid = await require_visible_user(admin, body.user_uuid)
        result = await actions_mod.execute(
            db, action_id=action_id, user_uuid=user_uuid, params=body.params
        )
        await store.log_action(
            db,
            admin_username=getattr(admin, "username", None),
            target_user_uuid=user_uuid,
            action_id=action_id,
            triggered_by_rule_id=body.triggered_by_rule_id,
            ok=result["ok"],
            message=result["message"],
            params=body.params,
        )
        ctx.events.emit("action_executed", {
            "action_id": action_id,
            "user_uuid": user_uuid,
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
        admin: Any = Depends(can_view),
    ) -> SessionListResponse:
        user_uuid = await require_visible_user(admin, user_uuid)
        items, total = await store.sessions_for_user(db, user_uuid, limit, offset)
        return SessionListResponse(items=items, total=total)

    @router.get("/sessions/recent", response_model=SessionListResponse)
    async def sessions_recent(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        action_id: Optional[str] = None,
        admin_username: Optional[str] = None,
        admin: Any = Depends(can_view),
    ) -> SessionListResponse:
        visible = await visible_user_uuids(admin)
        items, total = await store.sessions_recent(
            db, limit=limit, offset=offset,
            action_id=action_id, admin_username=admin_username,
            visible_user_uuids=visible,
        )
        return SessionListResponse(items=items, total=total)

    return router
