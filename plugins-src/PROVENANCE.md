# Local plugin compatibility and provenance

This compatibility pass is based on the official Remnawave Admin `4.6.2`
release (`f43293ff5c60313bb85f774dff5961d19b67fd90`) merged into this tree.
The relevant upstream contracts are:

- `alembic/versions/20260823_0102_user_throttles.py` for the optional
  `user_throttles` table;
- `shared/agent_version.py` for the required Node Agent `1.7.3` baseline;
- `web/backend/api/v2/violations.py` for
  `violation.throttle.add` / `violation.throttle.remove` audit events; and
- `web/backend/api/v2/nodes.py` plus the audit middleware for explicit
  `node.restart` / `nodes.restart` maintenance evidence.

Adapted local plugin versions:

| Plugin | Version | Compatibility behavior |
|---|---:|---|
| Smart Support | 1.4.4 | Read-only active throttle context; safe when migration 0102 is absent |
| Retention Radar | 1.2.4 | Active throttles are excluded from live and dry-run recipient sets |
| Local Block Radar | 0.7.4 | Agent 1.7.3 warning, explicit-restart suppression, hardened Sonnet contract |
| Incident Center | 0.1.2 | Rollout/audit context and evidence-only throttle restore alerts |

An unavailable or malformed AI response never changes the deterministic Block
Radar incident. Incident Center does not infer maintenance or a squad restore
failure: it requires explicit audit evidence. These plugins do not replace or
fork the upstream throttle API.
