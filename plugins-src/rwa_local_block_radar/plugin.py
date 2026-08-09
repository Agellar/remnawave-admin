"""Plugin API v1 manifest for the self-hosted radar."""
from __future__ import annotations

import asyncio

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask
from rwa_incident_hub import ensure_schema as ensure_incident_schema

from . import __version__, ai, engine, settings as settings_mod, store
from .api import RBAC_RESOURCES, build_router


def _build(ctx: PluginContext) -> PluginParts:
    state: dict = {
        "schema_ready": False,
        "last_tick": None,
        "new_alert_ids": [],
        "ai_tasks": {},
    }

    async def analyze_in_background(alert_id: int) -> None:
        try:
            result = await ai.analyze_alert(ctx, alert_id)
            ctx.logger.info(
                "local_block_radar.ai_analysis_ready",
                extra={
                    "alert_id": alert_id,
                    "classification": result["classification"],
                    "confidence": result["confidence"],
                    "model": result["model"],
                },
            )
        except Exception:
            ctx.logger.warning(
                "local_block_radar.ai_analysis_failed",
                extra={"alert_id": alert_id},
                exc_info=True,
            )
        finally:
            state["ai_tasks"].pop(alert_id, None)

    async def tick() -> None:
        if not state["schema_ready"]:
            await store.ensure_schema(ctx.db)
            await ensure_incident_schema(ctx.db)
            state["schema_ready"] = True
            ctx.logger.info("local_block_radar.schema_ready")
        try:
            await engine.run_tick(ctx, state)
            cfg = await settings_mod.get(ctx.settings)
            if cfg["ai_enabled"] and cfg["ai_auto_analyze"]:
                for alert_id in state.pop("new_alert_ids", []):
                    if alert_id in state["ai_tasks"]:
                        continue
                    state["ai_tasks"][alert_id] = asyncio.create_task(
                        analyze_in_background(alert_id)
                    )
        except Exception as exc:
            state["last_tick"] = {"ok": False, "error": str(exc)}
            ctx.logger.warning("local_block_radar.tick_failed", exc_info=True)

    return PluginParts(
        router=build_router(ctx, state),
        scheduled_tasks=[ScheduledTask(name="local-radar", interval_seconds=60, coro=tick)],
    )


def manifest() -> PluginManifest:
    return PluginManifest(
        id="block_radar",
        name="Local Block Radar",
        version=__version__,
        billing="free",
        build=_build,
        rbac_resources=RBAC_RESOURCES,
        navigation=[
            NavEntry(
                path="/plugins/block-radar",
                label_i18n="plugins.block_radar.nav",
                icon="Activity",
                section_i18n="nav.sections.plugins",
            )
        ],
    )
