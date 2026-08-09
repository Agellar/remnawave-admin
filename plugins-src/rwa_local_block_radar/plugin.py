"""Plugin API v1 manifest for the self-hosted radar."""
from __future__ import annotations

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask

from . import __version__, engine, store
from .api import RBAC_RESOURCES, build_router


def _build(ctx: PluginContext) -> PluginParts:
    state: dict = {"schema_ready": False, "last_tick": None}

    async def tick() -> None:
        if not state["schema_ready"]:
            await store.ensure_schema(ctx.db)
            state["schema_ready"] = True
            ctx.logger.info("local_block_radar.schema_ready")
        try:
            await engine.run_tick(ctx, state)
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
