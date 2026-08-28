"""Evidence-only maintenance, rollout and throttle operational context."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from shared.agent_version import LATEST_AGENT_VERSION

from . import store


MIN_COMPATIBLE_AGENT_VERSION = LATEST_AGENT_VERSION
AUDIT_ACTIONS = (
    "node.restart",
    "nodes.restart",
    "violation.throttle.add",
    "violation.throttle.remove",
)
MAX_AUDIT_DETAILS_CHARS = 16_384


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)", str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def _details(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > MAX_AUDIT_DETAILS_CHARS:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def restore_failure_evidence(details: Any) -> dict[str, bool] | None:
    """Recognize only explicit restore-failure evidence.

    The current 4.6.2 event can contain only ``squads_restored=false``.  That
    also means that there were no previous squads to restore, so it is
    deliberately insufficient by itself.
    """
    parsed = _details(details)
    if parsed.get("restore_failed") is True:
        return {"restore_failed": True}
    if (
        parsed.get("restore_required") is True
        and parsed.get("squads_restored") is False
    ):
        return {"restore_required": True, "squads_restored": False}
    return None


def _at(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


async def operational_context(db, *, hours: int = 24) -> dict[str, Any]:
    """Return sanitized operational evidence and remain safe on old schemas."""
    nodes_exists = await db.fetchval("SELECT to_regclass($1)", "public.nodes")
    nodes = []
    if nodes_exists:
        nodes = await db.fetch(
            """SELECT uuid::text AS node_uuid, name, agent_version
                 FROM nodes WHERE NOT COALESCE(is_disabled, false)
                 ORDER BY name"""
        )

    minimum = _version_tuple(MIN_COMPATIBLE_AGENT_VERSION)
    versions: dict[str, int] = {}
    compatible = 0
    outdated: list[dict[str, str]] = []
    unknown: list[str] = []
    node_names: dict[str, str] = {}
    for raw in nodes:
        row = dict(raw)
        node_uuid = str(row.get("node_uuid") or "")
        name = str(row.get("name") or "node")
        node_names[node_uuid] = name
        version = str(row.get("agent_version") or "").strip()
        parsed = _version_tuple(version)
        if parsed is None:
            unknown.append(name)
            versions["unknown"] = versions.get("unknown", 0) + 1
        else:
            versions[version] = versions.get(version, 0) + 1
            if minimum is not None and parsed < minimum:
                outdated.append({"node_name": name, "agent_version": version})
            else:
                compatible += 1

    result: dict[str, Any] = {
        "agent_rollout": {
            "source_state": "available" if nodes_exists else "missing",
            "minimum_version": MIN_COMPATIBLE_AGENT_VERSION,
            "total": len(nodes),
            "compatible": compatible,
            "versions": versions,
            "outdated": outdated,
            "unknown": unknown,
            "complete": bool(nodes) and compatible == len(nodes),
        },
        "audit": {
            "source_state": "missing",
            "window_hours": max(1, int(hours)),
            "throttle_added": 0,
            "throttle_removed": 0,
            "operator_restarts": [],
            "restore_failures": [],
        },
    }

    audit_exists = await db.fetchval(
        "SELECT to_regclass($1)", "public.admin_audit_log"
    )
    if not audit_exists:
        return result
    result["audit"]["source_state"] = "available"
    rows = await db.fetch(
        """SELECT id, action, resource_id, details, created_at
             FROM admin_audit_log
            WHERE action = ANY($1::text[])
              AND created_at >= NOW() - make_interval(hours => $2)
            ORDER BY id DESC""",
        list(AUDIT_ACTIONS),
        max(1, int(hours)),
    )
    for raw in rows:
        row = dict(raw)
        action = str(row.get("action") or "")
        if action == "violation.throttle.add":
            result["audit"]["throttle_added"] += 1
        elif action == "violation.throttle.remove":
            result["audit"]["throttle_removed"] += 1
            evidence = restore_failure_evidence(row.get("details"))
            if evidence is not None:
                result["audit"]["restore_failures"].append(
                    {
                        "audit_event_id": int(row["id"]),
                        "observed_at": _at(row.get("created_at")),
                        "evidence": evidence,
                    }
                )
        elif action in {"node.restart", "nodes.restart"}:
            resource_id = str(row.get("resource_id") or "")
            result["audit"]["operator_restarts"].append(
                {
                    "audit_event_id": int(row["id"]),
                    "node_name": node_names.get(resource_id),
                    "observed_at": _at(row.get("created_at")),
                }
            )
    return result


async def reconcile_restore_failures(db) -> dict[str, Any]:
    """Create incidents only for audit rows carrying explicit failure proof."""
    await store.ensure_schema(db)
    context = await operational_context(db)
    failures = context["audit"]["restore_failures"]
    for failure in failures:
        audit_id = int(failure["audit_event_id"])
        await store.upsert(
            db,
            source_plugin="incident_center",
            incident_key=f"throttle-restore-audit:{audit_id}",
            kind="throttle_restore_failed",
            severity="high",
            title="Не восстановлены прежние сквады после снятия ограничения",
            details={
                "audit_event_id": audit_id,
                "observed_at": failure["observed_at"],
                "evidence": failure["evidence"],
            },
        )
    return {
        "restore_failures": len(failures),
        "audit_source_state": context["audit"]["source_state"],
    }
