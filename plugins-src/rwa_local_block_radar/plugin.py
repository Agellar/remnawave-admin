"""Plugin API v1 manifest for the self-hosted radar."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask
from rwa_incident_hub import ensure_schema as ensure_incident_schema

from . import __version__, ai, engine, probes, settings as settings_mod, store
from .api import RBAC_RESOURCES, build_router


def _build(ctx: PluginContext) -> PluginParts:
    schema_lock = asyncio.Lock()
    probe_lock = asyncio.Lock()
    state: dict = {
        "schema_ready": False,
        "last_tick": None,
        "last_probe": None,
        "last_probe_at": None,
        "next_probe_at_epoch": time.time(),
        "probe_interval_seconds": 60,
        "probe_running": False,
        "manual_cooldown_until": 0.0,
        "probe_cursor": 0,
        "new_alert_ids": [],
        "ai_tasks": {},
    }

    async def ensure_schema() -> None:
        if state["schema_ready"]:
            return
        async with schema_lock:
            if state["schema_ready"]:
                return
            await store.ensure_schema(ctx.db)
            await ensure_incident_schema(ctx.db)
            state["schema_ready"] = True
            ctx.logger.info("local_block_radar.schema_ready")

    async def run_probe(*, manual: bool = False) -> dict:
        if state["probe_running"] or probe_lock.locked():
            raise RuntimeError("probe_already_running")
        async with probe_lock:
            await ensure_schema()
            cfg = await settings_mod.get(ctx.settings)
            if not cfg["globalping_enabled"]:
                raise RuntimeError("globalping_disabled")

            state["probe_running"] = True
            if manual:
                state["manual_cooldown_until"] = time.time() + 15
            try:
                result = await probes.run_cycle(ctx, state, cfg)
                state["last_probe"] = result
                return result
            except Exception:
                state["last_probe"] = {"ok": False, "error": "probe_cycle_failed"}
                ctx.logger.warning("local_block_radar.probe_cycle_failed", exc_info=True)
                raise
            finally:
                completed_at = time.time()
                interval = int(cfg["probe_interval_seconds"])
                state["last_probe_at"] = datetime.fromtimestamp(
                    completed_at, tz=timezone.utc
                ).isoformat()
                state["probe_interval_seconds"] = interval
                state["next_probe_at_epoch"] = completed_at + interval
                state["probe_running"] = False

    state["run_probe"] = run_probe

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

    async def radar_tick() -> None:
        await ensure_schema()
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

    async def probe_tick() -> None:
        await ensure_schema()
        cfg = await settings_mod.get(ctx.settings)
        interval = int(cfg["probe_interval_seconds"])
        state["probe_interval_seconds"] = interval
        if not cfg["globalping_enabled"]:
            state["next_probe_at_epoch"] = None
            return
        if state["probe_running"]:
            return
        due_at = float(state.get("next_probe_at_epoch") or 0)
        if time.time() < due_at:
            return
        try:
            await run_probe()
        except RuntimeError as exc:
            if str(exc) != "probe_already_running":
                raise

    return PluginParts(
        router=build_router(ctx, state),
        scheduled_tasks=[
            ScheduledTask(name="local-radar", interval_seconds=60, coro=radar_tick),
            ScheduledTask(name="globalping", interval_seconds=5, coro=probe_tick),
        ],
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
