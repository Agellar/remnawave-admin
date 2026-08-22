"""Роуты плагина.

Авторизацию плагин объявляет сам: загрузчик вешает на роутер только гейт
лицензии, и то лишь платным плагинам. Без явной зависимости ручки были бы
открыты всем, кто дотянулся до бэкенда.
"""
from __future__ import annotations

import re
from math import ceil

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

# Персональные данные (IP, логины, Telegram ID) не должны оседать в кэшах
# браузера и прокси — все JSON-ответы отдаём с no-store.
_NO_STORE = {"Cache-Control": "private, no-store", "Pragma": "no-cache"}


def _json(payload: dict, status: int = 200) -> JSONResponse:
    """JSONResponse с no-store. jsonable_encoder обязателен: прямой JSONResponse
    минует кодировщик FastAPI, а из БД могут прийти IPv4Address/datetime."""
    return JSONResponse(jsonable_encoder(payload), status_code=status, headers=_NO_STORE)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _can_view_users(admin) -> bool:
    """Mirror ``require_permission`` without a second/async RBAC lookup."""
    if getattr(admin, "account_id", None) is None or getattr(admin, "role", None) == "superadmin":
        return True
    checker = getattr(admin, "has_permission", None)
    return bool(checker and checker("live_flow", "view_users"))


def _principal_identity(admin) -> str:
    account_id = getattr(admin, "account_id", None)
    if account_id is not None:
        return f"account:{account_id}"
    # Legacy admins are privileged, but still share a bounded request budget
    # keyed by the authenticated principal rather than by a bearer token.
    return "legacy:{method}:{username}:{telegram}".format(
        method=getattr(admin, "auth_method", "unknown"),
        username=getattr(admin, "username", "unknown"),
        telegram=getattr(admin, "telegram_id", ""),
    )


def build_router(ctx):
    from fastapi import APIRouter, Depends, HTTPException, Query
    from fastapi.responses import HTMLResponse, Response

    from web.backend.core.plugin_api import auth_deps

    from .data import (
        DETAIL_DEFAULT_PAGE_SIZE,
        DETAIL_MAX_PAGE,
        DETAIL_MAX_PAGE_SIZE,
        GROUPS,
        collect,
        group_users,
        node_users,
    )
    from .module import MODULE_JS
    from .page import APP_JS, PAGE_HTML
    from .rate_limit import LIMITER
    from .scope import resolve_access_scope

    AdminUser, require_permission = auth_deps()
    router = APIRouter()

    async def _rate_guard(admin, bucket: str, *, limit: int, window_s: float) -> None:
        retry = await LIMITER.check(_principal_identity(admin), bucket, limit=limit, window_s=window_s)
        if retry > 0:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited"},
                headers={**_NO_STORE, "Retry-After": str(max(1, ceil(retry)))},
            )

    @router.get("/data", summary="Живой срез: ноды, онлайн, трафик, форма графа (live_flow:view)")
    async def data(
        admin: AdminUser = Depends(require_permission("live_flow", "view")),
    ):
        await _rate_guard(admin, "data", limit=12, window_s=10.0)
        scope = await resolve_access_scope(ctx, admin)
        payload = await collect(ctx, scope)
        payload["can_view_users"] = _can_view_users(admin)
        return _json(payload)

    # Списки людей (логин, IP, AS, Telegram ID, гео) — отдельное право
    # live_flow:view_users: просмотр схемы его не даёт.
    @router.get("/node/{node_uuid}/users", summary="Кто сейчас на ноде: пользователь, IP, AS (live_flow:view_users)")
    async def node_users_route(
        node_uuid: str,
        page: int = Query(1, ge=1, le=DETAIL_MAX_PAGE),
        limit: int = Query(DETAIL_DEFAULT_PAGE_SIZE, ge=1, le=DETAIL_MAX_PAGE_SIZE),
        admin: AdminUser = Depends(require_permission("live_flow", "view_users")),
    ):
        if not _UUID_RE.match(node_uuid):
            raise HTTPException(status_code=422, detail={"code": "bad_uuid"})
        await _rate_guard(admin, "users", limit=20, window_s=10.0)
        scope = await resolve_access_scope(ctx, admin)
        node_uuid = node_uuid.lower()
        if not scope.allows_node(node_uuid):
            raise HTTPException(status_code=404, detail={"code": "node_not_found"})
        data = await node_users(ctx, node_uuid, scope, page=page, limit=limit)
        if data is None:
            raise HTTPException(status_code=404, detail={"code": "node_not_found"})
        return _json(data)

    @router.get("/group/{group}/users", summary="Сводный список активных по типу сети: mobile | fixed | unknown | all (live_flow:view_users)")
    async def group_users_route(
        group: str,
        page: int = Query(1, ge=1, le=DETAIL_MAX_PAGE),
        limit: int = Query(DETAIL_DEFAULT_PAGE_SIZE, ge=1, le=DETAIL_MAX_PAGE_SIZE),
        admin: AdminUser = Depends(require_permission("live_flow", "view_users")),
    ):
        if group not in GROUPS:
            raise HTTPException(status_code=422, detail={"code": "bad_group"})
        await _rate_guard(admin, "users", limit=20, window_s=10.0)
        scope = await resolve_access_scope(ctx, admin)
        return _json(await group_users(ctx, group, scope, page=page, limit=limit))

    @router.get("/ui", response_class=HTMLResponse, summary="Страница со схемой")
    async def ui(
        _admin: AdminUser = Depends(require_permission("live_flow", "view")),
    ):
        return HTMLResponse(PAGE_HTML)

    @router.get("/app", summary="JS страницы (CSP script-src 'self'; без .js — его перехватывает статик-локация nginx фронта)")
    async def app_js(
        _admin: AdminUser = Depends(require_permission("live_flow", "view")),
    ):
        return Response(APP_JS, media_type="application/javascript; charset=utf-8")

    @router.get("/ui-module", summary="UI-модуль для generic-маршрута /plugins/:pluginId (window.rwaPluginUI)")
    async def ui_module(
        _admin: AdminUser = Depends(require_permission("live_flow", "view")),
    ):
        # Без расширения .js — иначе перехватит статик-локация nginx фронта.
        # Авторизация — кукой rw_access: <script src> того же origin её шлёт.
        return Response(MODULE_JS, media_type="application/javascript; charset=utf-8")

    return router
