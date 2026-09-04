# Local plugin compatibility and provenance

This compatibility pass is based on the official Remnawave Admin `4.7.1`
release (`a3bc5a63191665ac13c59b40ba2a466f34157e04`) merged into this tree.
The relevant upstream contracts are:

- `shared/db/connections.py` and `web/backend/api/v2/collector.py` for the
  4.7.1 connection model: `connected_at` remains the session start rather
  than being rewritten as a heartbeat;
- `users.raw_data.userTraffic.onlineAt` together with
  `lastConnectedNodeUuid` for bounded current-user activity, and
  `nodes.users_online` for the panel-maintained current node population;
- `alembic/versions/20260823_0102_user_throttles.py` for the optional
  `user_throttles` table;
- `shared/agent_version.py` for the required Node Agent `1.8.0` baseline;
- `shared/config_service.py` for the recap window and torrent peer/ASN policy;
- `shared/db/violations.py` for separate non-annulled and annulled counts;
- `web/backend/api/v2/violations.py` for
  `violation.throttle.add` / `violation.throttle.remove` audit events; and
- `web/backend/api/v2/nodes.py` plus the audit middleware for explicit
  `node.restart` / `nodes.restart` maintenance evidence.

Adapted local plugin versions:

| Plugin | Version | Compatibility behavior |
|---|---:|---|
| Smart Support | 1.4.6 | Long-lived current sessions use validated `onlineAt` + node identity; recent node history remains visible; cluster share uses `nodes.users_online` |
| Retention Radar | 1.2.6 | Incident suppression combines bounded current-user activity with recent connection history; active throttles and live-send safety gates retained |
| Local Block Radar | 0.7.6 | Transport labels prefer validated current-user/node evidence and fall back to recent starts; Sonnet remains explanatory only |
| Incident Center | 0.1.3 | Explicit false-positive reviews no longer suppress support/retention; snoozes and unclear reviews still do |
| Live Flow | 0.17.0+agellar.2 | Official v0.17.0 remains current; local RBAC, scope, limits, and performance changes retained |

An unavailable or malformed AI response never changes the deterministic Block
Radar incident. Incident Center does not infer maintenance or a squad restore
failure: it requires explicit audit evidence. These plugins do not replace or
fork the upstream throttle API.

Official Live Flow release/tag/main were rechecked on 2026-08-28 and remain
`c84cadde9aa2f31e70ebbd32bc1ebb0ba3d18b49`. Its security regression suite passes
against Admin 4.7.1; no artificial upstream version bump was made.

The official Smart Support/Retention catalogue at
`https://license.nexuslink.ru/v1/catalog` timed out from both the workstation
and production host on 2026-08-28. These local implementations were checked
against the public Admin contracts; this does not claim a comparison with
unavailable closed-source plugin releases.

Additional fork safeguards in this release:

- Torrent evidence counts canonical IPs, not endpoint strings/ports. Both
  thresholds use the same filtered window. Unavailable or incomplete evidence
  defers a verdict instead of accusing a user. Raw history remains available.
- The user timeline is paginated in PostgreSQL with one time window for all
  sources, stable ordering, full counts, and payload loading after LIMIT.
- UI errors are distinct from empty history, switching users resets pagination,
  and annulled detections are labelled separately. Chart series do not animate
  on each periodic refresh.
- Guarded Node Agent 1.8.0 scripts pin the official release image digest and
  verify Collector reachability, exact Compose/container identity, health,
  nDPI-off state, and target/rollback image IDs before accepting a rollout.
