"""Local node-share baseline and dip detector."""
from __future__ import annotations

from datetime import datetime, timezone
from rwa_incident_hub import resolve as resolve_incident, upsert as upsert_incident

from . import settings as settings_mod, store

MIN_BASELINE_HOURS = 5
MIN_BASELINE_SAMPLES = 60
RESTART_SUPPRESSION_MINUTES = 10


def classify_transport(tag: str | None) -> str:
    value = (tag or "").strip().lower()
    if not value:
        return "mixed"
    if "xhttp" in value or "splithttp" in value:
        return "reality+xhttp" if "reality" in value else "xhttp"
    if "httpupgrade" in value:
        return "httpupgrade"
    if "reality" in value:
        return "reality"
    if "websocket" in value or "-ws" in value or value.endswith("ws"):
        return "ws"
    for needle, label in (
        ("grpc", "grpc"), ("trojan", "trojan"), ("shadowsocks", "ss"),
        ("kcp", "kcp"), ("quic", "quic"), ("h2", "h2"),
        ("tls", "tls"), ("raw", "raw"),
    ):
        if needle in value:
            return label
    return value[:64] or "other"


async def current_nodes(db, window_minutes: int) -> list[dict]:
    nodes = await db.fetch(
        """SELECT uuid::text AS node_uuid, name AS node_name,
                  COALESCE(users_online, 0)::int AS online,
                  COALESCE(is_connected, false) AS node_alive,
                  COALESCE(agent_version, 'unknown') AS agent_version,
                  COALESCE(raw_data::jsonb #>> '{provider,name}', name) AS provider_name
           FROM nodes WHERE NOT COALESCE(is_disabled, false)
           ORDER BY name"""
    )
    tags = await db.fetch(
        """SELECT node_uuid::text AS node_uuid,
                  device_info->>'inbound_tag' AS inbound_tag, COUNT(*)::int AS hits
           FROM user_connections
           WHERE connected_at >= NOW() - make_interval(mins => $1)
             AND COALESCE(device_info->>'inbound_tag', '') <> ''
           GROUP BY node_uuid, device_info->>'inbound_tag'
           ORDER BY node_uuid, hits DESC""",
        window_minutes,
    )
    primary: dict[str, str] = {}
    for item in tags:
        primary.setdefault(str(item["node_uuid"]), str(item["inbound_tag"] or ""))

    total = sum(int(row["online"] or 0) for row in nodes if row["node_alive"])
    result = []
    for raw in nodes:
        row = dict(raw)
        row["transport"] = classify_transport(primary.get(row["node_uuid"]))
        row["total_online"] = total
        row["share"] = (row["online"] / total) if total > 0 else 0.0
        result.append(row)
    return result


def _baseline_ready(base: dict | None) -> bool:
    if not base or int(base.get("samples") or 0) < MIN_BASELINE_SAMPLES:
        return False
    first = base.get("first_at")
    if not first:
        return False
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - first).total_seconds() >= MIN_BASELINE_HOURS * 3600


def _is_dip(sample: dict, base: dict, cfg: dict) -> bool:
    frac = float(cfg["dip_frac"])
    return (
        float(base["online"] or 0) >= int(cfg["dip_min_users"])
        and float(sample["share"]) <= float(base["share"] or 0) * (1.0 - frac)
        and int(sample["online"]) <= float(base["online"] or 0) * (1.0 - frac * 0.50)
    )


def _is_recovered(sample: dict, base: dict, cfg: dict) -> bool:
    frac = float(cfg["dip_frac"])
    return (
        float(sample["share"]) >= float(base["share"] or 0) * (1.0 - frac * 0.35)
        or int(sample["online"]) >= float(base["online"] or 0) * (1.0 - frac * 0.25)
    )


async def recent_restart_nodes(
    db, node_uuids: list[str], minutes: int = RESTART_SUPPRESSION_MINUTES
) -> set[str]:
    """Return only nodes with an explicit successful restart audit event.

    Both action spellings exist because the endpoint writes ``node.restart``
    and the audit middleware writes ``nodes.restart``.  A missing legacy audit
    table fails open for detection rather than guessing that maintenance exists.
    """
    values = sorted({str(value) for value in node_uuids if value})
    if not values:
        return set()
    exists = await db.fetchval("SELECT to_regclass($1)", "public.admin_audit_log")
    if not exists:
        return set()
    rows = await db.fetch(
        """SELECT DISTINCT resource_id
             FROM admin_audit_log
            WHERE action = ANY($1::text[])
              AND resource='nodes'
              AND resource_id = ANY($2::text[])
              AND created_at >= NOW() - make_interval(mins => $3)""",
        ["node.restart", "nodes.restart"],
        values,
        max(1, int(minutes)),
    )
    return {str(row["resource_id"]) for row in rows if row["resource_id"]}


async def run_tick(ctx, state: dict) -> None:
    cfg = await settings_mod.get(ctx.settings)
    rows = await current_nodes(ctx.db, int(cfg["online_window_minutes"]))
    await store.insert_samples(ctx.db, rows)
    await store.cleanup(ctx.db, int(cfg["dip_history_days"]))
    restart_nodes = await recent_restart_nodes(
        ctx.db, [str(row["node_uuid"]) for row in rows]
    )

    created = resolved = measured = restart_suppressed = 0
    new_alert_ids: list[int] = []
    for row in rows:
        base = await store.baseline(ctx.db, row["node_uuid"], int(cfg["dip_history_days"]))
        opened = await store.open_alert(ctx.db, row["node_uuid"])
        if not _baseline_ready(base):
            continue
        measured += 1
        recent = await store.recent_samples(ctx.db, row["node_uuid"], int(cfg["dip_confirm_ticks"]))

        if opened:
            if len(recent) >= int(cfg["dip_confirm_ticks"]) and all(
                _is_recovered(sample, base, cfg) for sample in recent
            ):
                await store.resolve_alert(ctx.db, int(opened["id"]))
                await resolve_incident(
                    ctx.db,
                    source_plugin="block_radar",
                    incident_key=f"node:{row['node_uuid']}",
                )
                resolved += 1
                if cfg["notify_enabled"] and cfg["notify_resolved"]:
                    await _notify(ctx, row, base, resolved_event=True)
            else:
                await _publish_incident(ctx, row, base, alert_id=int(opened["id"]))
            continue

        if not cfg["dip_enabled"]:
            continue
        if str(row["node_uuid"]) in restart_nodes:
            restart_suppressed += 1
            continue
        if not row["node_alive"] and not cfg["dip_notify_offline"]:
            continue
        if len(recent) < int(cfg["dip_confirm_ticks"]):
            continue
        if all(_is_dip(sample, base, cfg) for sample in recent):
            alert_id = await store.create_alert(ctx.db, row, base)
            if alert_id:
                await _publish_incident(ctx, row, base, alert_id=alert_id)
                new_alert_ids.append(alert_id)
                created += 1
                if cfg["notify_enabled"]:
                    await _notify(
                        ctx, row, base, resolved_event=False, alert_id=alert_id
                    )

    state["last_tick"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "note": (
            "Globalping receives only configured public endpoint IP and port; "
            "user traffic and identities stay local"
            if cfg["globalping_enabled"]
            else "local node telemetry only"
        ),
        "external_monitoring": bool(cfg["globalping_enabled"]),
        "nodes_total": len(rows),
        "links_active": len(rows),
        "cells": len(rows),
        "accepted": len(rows),
        "rejected": 0,
        "alerts_locked": False,
        "alerts_new": created,
        "alerts_resolved": resolved,
        "notified": 0 if not cfg["notify_enabled"] else created + resolved,
        "measured": measured,
        "restart_suppressed": restart_suppressed,
    }
    state["new_alert_ids"] = new_alert_ids


async def _publish_incident(
    ctx, row: dict, base: dict, *, alert_id: int | None = None
) -> None:
    """Expose a privacy-safe infrastructure incident to sibling plugins."""
    online = int(row["online"] or 0)
    baseline_online = float(base["online"] or 0)
    drop_percent = round(max(0.0, 1.0 - online / baseline_online) * 100) if baseline_online else 0
    analysis = await store.analysis_for_alert(ctx.db, alert_id) if alert_id else None
    details = {
        "node_name": row["node_name"],
        "provider_name": row["provider_name"],
        "online": online,
        "baseline_online": round(baseline_online, 1),
        "drop_percent": drop_percent,
    }
    if analysis and analysis.get("availability", "available") == "available":
        details["ai_classification"] = analysis["classification"]
        details["ai_confidence"] = round(float(analysis["confidence"]), 2)
        details["ai_summary"] = analysis["summary"]
        details["ai_model"] = analysis["model"]
    await upsert_incident(
        ctx.db,
        source_plugin="block_radar",
        incident_key=f"node:{row['node_uuid']}",
        kind="node_transport_dip",
        severity="high" if drop_percent >= 70 else "medium",
        title=f"Просадка {row['node_name']} · {row['transport']}",
        details=details,
        node_uuid=row["node_uuid"],
        transport=row["transport"],
    )


async def _notify(
    ctx,
    row: dict,
    base: dict,
    *,
    resolved_event: bool,
    alert_id: int | None = None,
) -> None:
    from web.backend.core.plugin_api import panel_notify

    title = "Локальный радар: восстановление" if resolved_event else "Локальный радар: просадка"
    body = (
        f"Нода: <b>{row['node_name']}</b>\n"
        f"Провайдер: <b>{row['provider_name']}</b>\n"
        f"Онлайн: <b>{row['online']}</b>, норма: <b>{float(base['online']):.0f}</b>\n"
        f"Транспорт: <code>{row['transport']}</code>"
    )
    await panel_notify(
        title=title,
        body=body,
        severity="info" if resolved_event else "warning",
        link="/plugins/block-radar",
        plugin_id=ctx.plugin_id,
        group_key=f"local-block-radar:{row['node_uuid']}",
        actions=(
            [
                {"text": "✅ Блок был", "action": "fb_yes", "ref": str(alert_id)},
                {"text": "❌ Ложная тревога", "action": "fb_no", "ref": str(alert_id)},
            ]
            if not resolved_event and alert_id is not None
            else None
        ),
    )
