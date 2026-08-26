"""Plugin manifest for the unified Incident Center."""
from __future__ import annotations

from web.backend.core.plugin_api import PluginContext
from web.backend.core.plugins import NavEntry, PluginManifest, PluginParts, ScheduledTask

from . import __version__, operations, store
from .api import RBAC_RESOURCES, build_router


def _build(ctx: PluginContext) -> PluginParts:
    async def ensure() -> None:
        await operations.reconcile_restore_failures(ctx.db)

    return PluginParts(
        router=build_router(ctx),
        scheduled_tasks=[ScheduledTask(name="incident-center-schema", interval_seconds=300, coro=ensure)],
    )


def manifest() -> PluginManifest:
    return PluginManifest(
        id="incident_center",
        name="Incident Center",
        version=__version__,
        billing="free",
        build=_build,
        rbac_resources=RBAC_RESOURCES,
        navigation=[
            NavEntry(
                path="/plugins/incident-center",
                label_i18n="plugins.incident_center.nav",
                icon="ShieldAlert",
                permission=("incident_center", "view"),
                section_i18n="nav.sections.plugins",
            )
        ],
    )
