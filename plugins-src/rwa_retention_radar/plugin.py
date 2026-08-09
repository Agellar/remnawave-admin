"""Манифест плагина (Plugin API v1).

Точка входа: ``manifest()``. В проде регистрируется entry point-ом
``rwa.plugin``, в разработке — через
``RWA_DEV_PLUGINS=rwa_retention_radar.plugin:manifest``.
"""
from __future__ import annotations

import time

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask
from rwa_incident_hub import ensure_schema as ensure_incident_schema

from . import __version__, data, store
from .api import RBAC_RESOURCES, build_router
from .settings import get_thresholds

# Тик воркера. Реальный период пересчёта берётся из настроек
# (snapshot_recompute_seconds) — так оператор меняет его без рестарта.
WORKER_TICK_SECONDS = 60


def _build(ctx: PluginContext) -> PluginParts:
    state = {"schema_ready": False, "last_run": 0.0}

    async def snapshot_worker() -> None:
        """Пересчёт размеров сегментов и запись дневного среза.

        Схему создаём здесь же на первом тике: у плагина нет своей миграции
        в alembic панели, а ``build(ctx)`` синхронный и до event loop-а
        обратиться к БД не может.
        """
        if not state["schema_ready"]:
            await store.ensure_schema(ctx.db)
            await ensure_incident_schema(ctx.db)
            state["schema_ready"] = True
            ctx.logger.info("retention_radar.schema_ready")

        thresholds = await get_thresholds(ctx.settings)
        period = float(thresholds["snapshot_recompute_seconds"])
        now = time.monotonic()
        if state["last_run"] and now - state["last_run"] < period:
            return
        state["last_run"] = now

        counts = await data.counts(ctx.db, thresholds)
        await store.save_snapshot(ctx.db, counts)
        ctx.logger.info("retention_radar.snapshot_saved", extra={"counts": counts})

    return PluginParts(
        router=build_router(ctx),
        scheduled_tasks=[
            ScheduledTask(
                name="snapshot",
                interval_seconds=WORKER_TICK_SECONDS,
                coro=snapshot_worker,
            )
        ],
    )


def manifest() -> PluginManifest:
    return PluginManifest(
        id="retention_radar",
        name="Retention Radar",
        version=__version__,
        billing="free",
        build=_build,
        rbac_resources=RBAC_RESOURCES,
        navigation=[
            NavEntry(
                path="/plugins/retention-radar",
                label_i18n="plugins.retention_radar.nav",
                icon="TrendingDown",
                section_i18n="nav.sections.plugins",
            ),
        ],
    )
