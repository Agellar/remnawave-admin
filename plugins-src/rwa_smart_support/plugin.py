"""Манифест плагина (Plugin API v1).

Точка входа: ``manifest()``. В проде регистрируется entry point-ом
``rwa.plugin``, в разработке — через ``RWA_DEV_PLUGINS=rwa_smart_support.plugin:manifest``.
"""
from __future__ import annotations

import time
from typing import Optional

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask
from rwa_incident_hub import ensure_schema as ensure_incident_schema

from . import __version__, data, store
from .actions import RBAC_RESOURCES
from .api import build_router
from .settings import get_thresholds

# Тик воркера. Реальный период пересчёта берётся из настроек
# (correlation_recompute_seconds) — так оператор меняет его без рестарта.
WORKER_TICK_SECONDS = 60


def _build(ctx: PluginContext) -> PluginParts:
    state = {"schema_ready": False, "last_run": 0.0}

    async def correlation_worker() -> None:
        """Пересчёт кластеров массовых проблем.

        Схему создаём здесь же на первом тике: у плагина нет своей миграции
        в alembic панели, а ``build(ctx)`` синхронный и до event loop-а
        обратиться к БД не может.
        """
        if not state["schema_ready"]:
            await store.ensure_schema(ctx.db)
            await ensure_incident_schema(ctx.db)
            state["schema_ready"] = True
            ctx.logger.info("smart_support.schema_ready")

        thresholds = await get_thresholds(ctx.settings)
        period = float(thresholds["correlation_recompute_seconds"])
        now = time.monotonic()
        if state["last_run"] and now - state["last_run"] < period:
            return
        state["last_run"] = now

        clusters = await data.compute_clusters(ctx.db, thresholds)
        await store.replace_correlations(
            ctx.db, clusters, thresholds["correlation_history_minutes"]
        )
        ctx.logger.info("smart_support.correlations_updated", extra={"clusters": len(clusters)})

    return PluginParts(
        router=build_router(ctx),
        scheduled_tasks=[
            ScheduledTask(
                name="correlations",
                interval_seconds=WORKER_TICK_SECONDS,
                coro=correlation_worker,
            )
        ],
    )


def manifest() -> PluginManifest:
    return PluginManifest(
        id="smart_support",
        name="Smart Support",
        version=__version__,
        billing="free",
        build=_build,
        rbac_resources=RBAC_RESOURCES,
        navigation=[
            NavEntry(
                path="/plugins/smart-support",
                label_i18n="plugins.smart_support.nav",
                icon="Stethoscope",
                permission=("smart_support", "view"),
                section_i18n="nav.sections.plugins",
            ),
        ],
    )
