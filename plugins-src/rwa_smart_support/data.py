"""Сбор данных отчёта из БД панели.

Всё читается из локального кэша панели (таблицы ``users``,
``user_connections``, ``ip_metadata``, ``nodes``, ...) — Panel API дёргаем
только на мутациях в actions.py. Так отчёт собирается за один заход в
Postgres и не зависит от доступности панели Remnawave.
"""
from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from rwa_incident_hub.freshness import history_status

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# UA мониторинга и утилит: они дёргают ссылку подписки, но клиентом юзера
# не являются — иначе «последнее приложение» у всех станет Xray-Checker.
NON_CLIENT_UA = re.compile(r"xray-checker|uptime|curl|wget|python-requests|go-http-client", re.I)

# Оболочки, известные агрессивным энергосбережением: Android-производители,
# которые выгружают VPN из фона.
AGGRESSIVE_OEM = ("xiaomi", "redmi", "poco", "huawei", "honor", "oneplus",
                  "oppo", "realme", "vivo", "samsung", "meizu", "tecno", "infinix")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_panel_timestamp(value: Any) -> Optional[datetime]:
    """Parse a panel ISO timestamp without letting malformed cache data fail a report."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recent_panel_activity(value: Any, window_minutes: int) -> bool:
    """Trust ``onlineAt`` only inside a bounded window and small clock skew."""
    parsed = _parse_panel_timestamp(value)
    if parsed is None:
        return False
    age_seconds = (_now() - parsed).total_seconds()
    return -30.0 <= age_seconds <= max(1, int(window_minutes)) * 60.0


def is_globally_routable_ip(value: Any) -> bool:
    """Only public unicast addresses may contribute to abuse heuristics."""
    if value is None:
        return False
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        # PostgreSQL ``inet::text`` may include /32 or /128.
        try:
            address = ipaddress.ip_interface(str(value).strip()).ip
        except ValueError:
            return False
    # ``is_global`` excludes private, loopback, link-local, documentation and
    # CGNAT (100.64.0.0/10); multicast is excluded explicitly for clarity.
    return bool(address.is_global and not address.is_multicast)


def detect_query_kind(q: str) -> str:
    q = q.strip()
    if UUID_RE.match(q):
        return "uuid"
    if "@" in q:
        return "email"
    try:
        ipaddress.ip_address(q)
        return "ip"
    except ValueError:
        pass
    if q.isdigit():
        return "telegram_id"
    # short_uuid панели — компактная строка без разделителей
    if re.fullmatch(r"[0-9a-zA-Z]{10,32}", q) and any(ch.isdigit() for ch in q):
        return "short_uuid"
    return "username"


_SEARCH_SELECT = """
SELECT u.uuid, u.short_uuid, u.username, u.email, u.telegram_id, u.status, u.expire_at,
       c.connected_at AS last_connection_at,
       m.country_name, m.asn, m.asn_org
FROM users u
LEFT JOIN LATERAL (
    SELECT ip_address, connected_at
    FROM user_connections
    WHERE user_uuid = u.uuid
      AND connected_at >= NOW() - INTERVAL '30 days'
      AND connected_at <= NOW() + INTERVAL '30 seconds'
    ORDER BY connected_at DESC LIMIT 1
) c ON TRUE
LEFT JOIN ip_metadata m ON m.ip_address = c.ip_address
"""


async def search_users(
    db,
    q: str,
    limit: int,
    visible_user_uuids: Optional[set[str]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Резолвер идентификатора: тип запроса определяем сами, при промахе
    откатываемся на поиск по никнейму/почте. Ограничение видимости входит в
    SQL до LIMIT, иначе скрытые строки вытесняли бы разрешённых пользователей
    из результата, а post-filter возвращал бы ложно пустой поиск."""
    kind = detect_query_kind(q)
    rows: List[Any] = []
    visible_arg = None if visible_user_uuids is None else sorted(visible_user_uuids)
    scope_sql = "($3::uuid[] IS NULL OR u.uuid = ANY($3::uuid[]))"

    if kind == "uuid":
        rows = await db.fetch(
            _SEARCH_SELECT + f" WHERE u.uuid = $1::uuid AND {scope_sql} LIMIT $2",
            q, limit, visible_arg,
        )
    elif kind == "short_uuid":
        rows = await db.fetch(
            _SEARCH_SELECT + f" WHERE u.short_uuid = $1 AND {scope_sql} LIMIT $2",
            q, limit, visible_arg,
        )
    elif kind == "telegram_id":
        rows = await db.fetch(
            _SEARCH_SELECT + f" WHERE u.telegram_id = $1 AND {scope_sql} LIMIT $2",
            int(q), limit, visible_arg,
        )
    elif kind == "email":
        rows = await db.fetch(
            _SEARCH_SELECT + f" WHERE u.email ILIKE $1 AND {scope_sql} LIMIT $2",
            q, limit, visible_arg,
        )
    elif kind == "ip":
        rows = await db.fetch(
            _SEARCH_SELECT + f"""
            WHERE EXISTS (
                SELECT 1 FROM user_connections uc
                WHERE uc.user_uuid = u.uuid AND uc.ip_address = $1
                  AND uc.connected_at >= NOW() - INTERVAL '30 days'
                  AND uc.connected_at <= NOW() + INTERVAL '30 seconds'
            )
              AND {scope_sql}
            ORDER BY c.connected_at DESC NULLS LAST LIMIT $2""",
            q, limit, visible_arg,
        )
    else:
        rows = await db.fetch(
            _SEARCH_SELECT + f"""
            WHERE (u.username ILIKE $1 OR u.email ILIKE $1 OR u.short_uuid ILIKE $1)
              AND {scope_sql}
            ORDER BY u.updated_at DESC NULLS LAST LIMIT $2""",
            f"%{q}%", limit, visible_arg,
        )

    if not rows and kind != "username":
        kind = "fallback"
        rows = await db.fetch(
            _SEARCH_SELECT + f"""
            WHERE (u.username ILIKE $1 OR u.email ILIKE $1 OR u.description ILIKE $1)
              AND {scope_sql}
            ORDER BY u.updated_at DESC NULLS LAST LIMIT $2""",
            f"%{q}%", limit, visible_arg,
        )

    hits = []
    for r in rows:
        asn_label = r["asn_org"] or (f"AS{r['asn']}" if r["asn"] else None)
        hits.append({
            "uuid": str(r["uuid"]),
            "short_uuid": r["short_uuid"],
            "username": r["username"],
            "email": r["email"],
            "telegram_id": r["telegram_id"],
            "status": r["status"],
            "expire_at": r["expire_at"],
            "last_connection_at": r["last_connection_at"],
            "last_country": r["country_name"],
            "last_asn": asn_label,
        })
    return kind, hits


# ── секции отчёта ────────────────────────────────────────────────

async def user_section(db, user_uuid: str) -> Optional[Dict[str, Any]]:
    row = await db.fetchrow("SELECT * FROM users WHERE uuid = $1::uuid", user_uuid)
    if row is None:
        return None

    raw = row["raw_data"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    raw = raw or {}

    squads = []
    for squad in raw.get("activeInternalSquads") or []:
        if isinstance(squad, dict) and squad.get("name"):
            squads.append(squad["name"])
        elif isinstance(squad, str):
            squads.append(squad)

    limit_bytes = row["traffic_limit_bytes"] or 0
    used_bytes = row["used_traffic_bytes"] or 0
    percent = round(used_bytes / limit_bytes * 100, 1) if limit_bytes else None

    expire_at = row["expire_at"]
    days_left = (expire_at - _now()).days if expire_at else None

    devices = await db.fetch(
        """SELECT d.hwid, d.platform, d.device_model, d.os_version, d.app_version,
                  GREATEST(d.updated_at, d.created_at) AS last_seen_at,
                  (b.hwid IS NOT NULL) AS is_blacklisted
           FROM user_hwid_devices d
           LEFT JOIN hwid_blacklist b ON b.hwid = d.hwid
           WHERE d.user_uuid = $1::uuid
             AND d.removed_at IS NULL
           ORDER BY last_seen_at DESC NULLS LAST""",
        user_uuid,
    )

    return {
        "uuid": str(row["uuid"]),
        "short_uuid": row["short_uuid"],
        "username": row["username"],
        "email": row["email"],
        "telegram_id": row["telegram_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expire_at": expire_at,
        "days_until_expire": days_left,
        "subscription_uuid": str(row["subscription_uuid"]) if row["subscription_uuid"] else None,
        "subscription_url": raw.get("subscriptionUrl"),
        "traffic": {"limit_bytes": limit_bytes or None, "used_bytes": used_bytes,
                    "percent": percent},
        "active_squads": squads,
        "hwid_limit": row["hwid_device_limit"],
        "hwid_devices": [
            {
                "hwid": d["hwid"],
                # Модель важнее чистой платформы: «OnePlus KB2003» говорит
                # оператору больше, чем «android».
                "platform": " ".join(x for x in (d["platform"], d["device_model"]) if x) or None,
                "last_seen_at": d["last_seen_at"],
                "is_blacklisted": bool(d["is_blacklisted"]),
            }
            for d in devices
        ],
        # не уходит во фронт, нужно правилам
        "_panel_user_id": row["id"],
        "_devices_raw": [dict(d) for d in devices],
    }


async def history_section(db, user_uuid: str, hours: int = 24) -> Dict[str, Any]:
    """Счётчики считаем отдельным агрегатом по всему окну, а таймлайн
    отдаём срезом: у активного юзера за сутки бывают сотни подключений, и
    гнать их все во фронт незачем — но и врать в счётчиках нельзя."""
    stats = await db.fetchrow(
        """SELECT COUNT(*) AS total,
                  COUNT(DISTINCT c.ip_address) AS ips,
                  COUNT(DISTINCT m.country_name) AS countries,
                  COUNT(DISTINCT m.asn) AS asns,
                  COUNT(DISTINCT c.node_uuid) AS nodes,
                  COUNT(*) FILTER (
                      WHERE c.disconnected_at IS NOT NULL
                        AND c.disconnected_at - c.connected_at < INTERVAL '60 seconds'
                  ) AS short_sessions,
                  ARRAY_REMOVE(ARRAY_AGG(DISTINCT m.country_name), NULL) AS country_names
           FROM user_connections c
           LEFT JOIN ip_metadata m ON m.ip_address = c.ip_address
           WHERE c.user_uuid = $1::uuid
             AND c.connected_at >= NOW() - make_interval(hours => $2)
             AND c.connected_at <= NOW() + INTERVAL '30 seconds'""",
        user_uuid, hours,
    )

    # География и число адресов считаются только по глобально маршрутизируемым
    # IP. Домашние подсети, loopback и CGNAT нельзя интерпретировать как смену
    # страны/провайдера или мультиаккаунтинг.
    identity_rows = await db.fetch(
        """SELECT DISTINCT c.ip_address::text AS ip_address,
                          m.country_name, m.asn
           FROM user_connections c
           LEFT JOIN ip_metadata m ON m.ip_address = c.ip_address
           WHERE c.user_uuid = $1::uuid
             AND c.connected_at >= NOW() - make_interval(hours => $2)
             AND c.connected_at <= NOW() + INTERVAL '30 seconds'""",
        user_uuid, hours,
    )
    public_rows = [r for r in identity_rows if is_globally_routable_ip(r["ip_address"])]
    public_ips = {str(r["ip_address"]) for r in public_rows}
    public_countries = {str(r["country_name"]) for r in public_rows if r["country_name"]}
    public_asns = {str(r["asn"]) for r in public_rows if r["asn"] is not None}
    anomaly_stats = dict(stats)
    anomaly_stats.update({
        "ips": len(public_ips),
        "countries": len(public_countries),
        "asns": len(public_asns),
        "country_names": sorted(public_countries),
    })

    rows = await db.fetch(
        """SELECT c.connected_at, c.disconnected_at, c.ip_address, c.node_uuid,
                  m.country_name, m.city, m.asn, m.asn_org,
                  n.name AS node_name
           FROM user_connections c
           LEFT JOIN ip_metadata m ON m.ip_address = c.ip_address
           LEFT JOIN nodes n ON n.uuid = c.node_uuid
           WHERE c.user_uuid = $1::uuid
             AND c.connected_at >= NOW() - make_interval(hours => $2)
             AND c.connected_at <= NOW() + INTERVAL '30 seconds'
           ORDER BY c.connected_at DESC
           LIMIT 100""",
        user_uuid, hours,
    )

    timeline = []
    for r in rows:
        public_ip = is_globally_routable_ip(r["ip_address"])
        duration = None
        if r["disconnected_at"]:
            duration = int((r["disconnected_at"] - r["connected_at"]).total_seconds())
        timeline.append({
            "connected_at": r["connected_at"],
            "disconnected_at": r["disconnected_at"],
            "duration_seconds": duration,
            "ip": r["ip_address"],
            "country": r["country_name"] if public_ip else None,
            "city": r["city"] if public_ip else None,
            "asn": f"AS{r['asn']}" if public_ip and r["asn"] else None,
            "asn_org": r["asn_org"] if public_ip else None,
            "node_uuid": str(r["node_uuid"]) if r["node_uuid"] else None,
            "node_name": r["node_name"],
        })

    return {
        "total_connections": int(stats["total"] or 0),
        "unique_ips": len(public_ips),
        "unique_countries": len(public_countries),
        "unique_asns": len(public_asns),
        "anomalies": _anomalies(anomaly_stats),
        "timeline": timeline,
    }


def _anomalies(stats) -> List[str]:
    """Человекочитаемые аномалии — их же потом читает LLM, поэтому пишем
    их фразами, а не кодами."""
    out: List[str] = []
    total = int(stats["total"] or 0)
    ips = int(stats["ips"] or 0)
    countries = list(stats["country_names"] or [])

    if total >= 80:
        out.append(f"Очень частые переподключения: {total} за 24 часа")

    # Два государства и до двенадцати адресов — нормальный профиль семьи с
    # несколькими устройствами, роумингом и VPN; не поднимаем гипотезу вовсе.
    if len(countries) >= 3 and ips >= 13:
        out.append(f"Подключения из {len(countries)} стран за сутки: {', '.join(sorted(countries))}")

    if ips >= 20:
        out.append(f"Много публичных IP-адресов: {ips} за сутки")

    short = int(stats["short_sessions"] or 0)
    if short >= 5:
        out.append(f"{short} сессий короче минуты — туннель рвётся сразу после подключения")

    return out


# Таблица истории запросов подписки наполняется синком панели, а не нами.
# Состояние воркера берём из sync_metadata: отсутствие новых запросов само по
# себе нормально и не означает, что источник сломан. Общий helper сохраняет
# MAX(request_at) как диагностический event timestamp и использует его только
# на старых установках, где metadata ещё отсутствует.
SOURCE_STALE_AFTER_MINUTES = 10


async def client_section(db, user_uuid: str, latest_versions: Dict[str, str]) -> Dict[str, Any]:
    rows = await db.fetch(
        """SELECT user_agent, request_at FROM subscription_request_history
           WHERE user_uuid = $1::uuid AND user_agent IS NOT NULL
             AND request_at <= NOW() + INTERVAL '30 seconds'
           ORDER BY request_at DESC LIMIT 50""",
        user_uuid,
    )
    freshness = await history_status(db, SOURCE_STALE_AFTER_MINUTES)
    stale = not bool(freshness["fresh"])
    newest_event = freshness.get("event_newest_at")

    picked = None
    for r in rows:
        if not NON_CLIENT_UA.search(r["user_agent"] or ""):
            picked = r
            break
    if picked is None:
        return {"last_app": None, "last_version": None, "raw_user_agent": None,
                "last_request_at": rows[0]["request_at"] if rows else None,
                "is_outdated": None, "days_since_last_request": None,
                "source_stale": stale, "source_newest_at": newest_event}

    app, version = parse_user_agent(picked["user_agent"])
    days = (_now() - picked["request_at"]).days if picked["request_at"] else None

    is_outdated = None
    if app and version:
        latest = latest_versions.get(app.lower())
        if latest:
            is_outdated = _version_tuple(version) < _version_tuple(latest)

    return {
        "last_app": app,
        "last_version": version,
        "raw_user_agent": picked["user_agent"],
        "last_request_at": picked["request_at"],
        "is_outdated": is_outdated,
        # Возраст считаем только по живым данным: на замерших он говорит
        # о поломке синка, а не о поведении клиента.
        "days_since_last_request": None if stale else days,
        "source_stale": stale,
        "source_newest_at": newest_event,
    }


def parse_user_agent(ua: str) -> Tuple[Optional[str], Optional[str]]:
    """``Happ/4.11.0/ios/2606031854651`` → ``("Happ", "4.11.0")``.

    Клиенты пишут UA кто во что горазд, поэтому разбираем консервативно:
    первый токен до пробела, дальше по слэшам, версией считаем только то,
    что похоже на версию.
    """
    if not ua:
        return None, None
    head = ua.strip().split(" ")[0]
    parts = head.split("/")
    app = parts[0] or None
    version = None
    for part in parts[1:]:
        if re.fullmatch(r"\d+(\.\d+)*", part):
            version = part
            break
    return app, version


def _version_tuple(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def platform_of(devices: List[Dict[str, Any]]) -> Optional[str]:
    for d in devices:
        label = " ".join(str(x) for x in (d.get("platform"), d.get("device_model")) if x)
        if label:
            return label
    return None


async def nodes_section(db, user_uuid: str, hours: int = 24) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """WITH touched AS (
           SELECT node_uuid,
                      MAX(connected_at) AS last_seen
               FROM user_connections
               WHERE user_uuid = $1::uuid
                 AND connected_at >= NOW() - make_interval(hours => $2)
                 AND connected_at <= NOW() + INTERVAL '30 seconds'
                 AND node_uuid IS NOT NULL
               GROUP BY node_uuid
           )
           SELECT n.uuid, n.name, n.address, n.is_connected, n.is_disabled,
                  n.cpu_usage, n.memory_usage, n.disk_usage, n.metrics_updated_at,
                  t.last_seen AS history_last_seen,
                  u.raw_data -> 'userTraffic' ->> 'onlineAt' AS online_at,
                  u.raw_data -> 'userTraffic' ->> 'lastConnectedNodeUuid'
                      AS live_node_uuid
             FROM nodes n
             LEFT JOIN touched t ON t.node_uuid = n.uuid
             LEFT JOIN users u ON u.uuid = $1::uuid
            WHERE t.node_uuid IS NOT NULL
               OR lower(n.uuid::text) = lower(
                      u.raw_data -> 'userTraffic' ->> 'lastConnectedNodeUuid'
                  )
            ORDER BY (
                         lower(n.uuid::text) = lower(
                             u.raw_data -> 'userTraffic' ->> 'lastConnectedNodeUuid'
                         )
                     ) DESC,
                     t.last_seen DESC NULLS LAST""",
        user_uuid, hours,
    )
    now = _now()
    out = []
    for r in rows:
        node_uuid = str(r["uuid"])
        live_node_uuid = str(r["live_node_uuid"] or "")
        live_here = (
            node_uuid.lower() == live_node_uuid.lower()
            and _recent_panel_activity(r["online_at"], 15)
        )
        # A stale/malformed raw snapshot must not create a phantom node outside
        # the requested connection-history window.
        if r["history_last_seen"] is None and not live_here:
            continue
        age = None
        if r["metrics_updated_at"]:
            age = int((now - r["metrics_updated_at"]).total_seconds())
        out.append({
            "uuid": node_uuid,
            "name": r["name"],
            "address": r["address"],
            "is_connected": bool(r["is_connected"]),
            "is_disabled": bool(r["is_disabled"]),
            "cpu_usage": r["cpu_usage"],
            "memory_usage": r["memory_usage"],
            "disk_usage": r["disk_usage"],
            "metrics_age_seconds": age,
            # Since Admin 4.7.1 ``connected_at`` is the start of a session,
            # not its heartbeat.  It keeps the node in history, while only
            # the panel's bounded current snapshot may mark it active.
            "user_active_here": live_here,
        })
    return out


async def throttle_section(db, user_uuid: str) -> Optional[Dict[str, Any]]:
    """Return the active administrative speed limit, when the migration exists.

    The plugin can be loaded while the panel upgrade is still between code
    rollout and migration 0102.  ``to_regclass`` keeps that window read-only
    and non-fatal instead of turning every support report into a 500.
    """
    exists = await db.fetchval("SELECT to_regclass($1)", "public.user_throttles")
    if str(exists or "") not in {"user_throttles", "public.user_throttles"}:
        return None
    row = await db.fetchrow(
        """SELECT rate_kbit, reason, created_at, until
             FROM user_throttles
            WHERE user_uuid = $1::uuid
              AND (until IS NULL OR until > NOW())""",
        user_uuid,
    )
    if not row:
        return None
    return {
        "active": True,
        "rate_kbit": int(row["rate_kbit"]),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "until": row["until"],
    }


def violation_recap_days() -> int:
    """Use the panel's 4.6.3 history window without trusting malformed config."""
    from shared.config_service import config_service

    raw = config_service.get("violation_recap_days", 30)
    try:
        days = int(raw) if not isinstance(raw, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 30
    # A broken setting must not turn a single support report into an
    # unbounded history scan. Report the effective window to the caller.
    return days if 1 <= days <= 365 else 30


async def violations_recap_section(db, user_uuid: str) -> Dict[str, Any]:
    """Count the complete window, separately from the bounded history sample.

    Match upstream's ``total`` / ``annulled`` semantics: total excludes
    annulled detector mistakes but still includes resolved history. Neither
    number is proof of a current restriction or of customer abuse.
    """
    days = violation_recap_days()
    row = await db.fetchrow(
        """SELECT COUNT(*) FILTER (
                      WHERE action_taken IS DISTINCT FROM 'annulled') AS total,
                  COUNT(*) FILTER (
                      WHERE COALESCE(action_taken, '') = '') AS unresolved,
                  COUNT(*) FILTER (
                      WHERE COALESCE(action_taken, '') NOT IN ('', 'annulled')) AS resolved,
                  COUNT(*) FILTER (
                      WHERE action_taken = 'annulled') AS annulled,
                  MAX(detected_at) FILTER (
                      WHERE action_taken IS DISTINCT FROM 'annulled') AS last_at
             FROM violations
            WHERE user_uuid = $1::uuid
              AND detected_at > NOW() - make_interval(days => $2)
              AND detected_at <= NOW() + INTERVAL '30 seconds'""",
        user_uuid, days,
    )
    return {
        "window_days": days,
        "total": int(row["total"] or 0) if row else 0,
        "unresolved": int(row["unresolved"] or 0) if row else 0,
        "resolved": int(row["resolved"] or 0) if row else 0,
        "annulled": int(row["annulled"] or 0) if row else 0,
        "last_at": row["last_at"] if row else None,
    }


async def violations_section(db, user_uuid: str, days: int = 14) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """SELECT id, detected_at, score, confidence, reasons, recommended_action,
                  action_taken
           FROM violations
           WHERE user_uuid = $1::uuid AND detected_at >= NOW() - make_interval(days => $2)
             AND detected_at <= NOW() + INTERVAL '30 seconds'
             AND action_taken IS DISTINCT FROM 'annulled'
           ORDER BY detected_at DESC LIMIT 20""",
        user_uuid, days,
    )
    out = []
    for r in rows:
        reasons = r["reasons"]
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except ValueError:
                reasons = [reasons]
        if isinstance(reasons, list):
            reason = "; ".join(str(x) for x in reasons[:3]) or None
        else:
            reason = str(reasons) if reasons else None
        out.append({
            "id": r["id"],
            "created_at": r["detected_at"],
            "score": float(r["score"]) if r["score"] is not None else None,
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            "reason": reason,
            # Keep ``action`` for older frontends, but expose the two official
            # semantics separately: a recommendation is not proof that an
            # administrative action was executed.
            "action": r["recommended_action"],
            "recommended_action": r["recommended_action"],
            "action_taken": r["action_taken"],
            "is_resolved": bool(r["action_taken"]),
        })
    return out


# ── фоновый расчёт корреляций ────────────────────────────────────

async def compute_clusters(db, thresholds: Dict[str, float]) -> List[Dict[str, Any]]:
    """Кластеры массовых проблем: спайк переподключений на одной ноде и
    несколько свежих нарушений у клиентов одного ASN.

    Считаем по всей базе разом раз в N секунд, а не на каждый отчёт —
    иначе каждый просмотр карточки стоил бы полного скана суток.
    """
    now = _now()
    clusters: List[Dict[str, Any]] = []

    node_window = int(thresholds["cluster_node_window_minutes"])
    node_rows = await db.fetch(
        """WITH recent AS (
               SELECT user_uuid, node_uuid, COUNT(*) AS cnt
               FROM user_connections
               WHERE connected_at >= NOW() - make_interval(mins => $1)
                 AND connected_at <= NOW() + INTERVAL '30 seconds'
                 AND node_uuid IS NOT NULL
               GROUP BY 1, 2
           )
           SELECT r.node_uuid, n.name,
                  COUNT(*) AS affected,
                  ARRAY_AGG(r.user_uuid::text) AS members,
                  COALESCE(n.users_online, 0)::int AS total_users
           FROM recent r LEFT JOIN nodes n ON n.uuid = r.node_uuid
           WHERE r.cnt >= $2
           GROUP BY r.node_uuid, n.name, n.users_online
           HAVING COUNT(*) >= $3""",
        node_window,
        int(thresholds["cluster_node_reconnects_per_user"]),
        int(thresholds["cluster_node_min_affected"]),
    )
    for r in node_rows:
        affected = int(r["affected"])
        # ``connected_at`` counts starts, so a long-lived online user may have
        # no row in the reconnect window.  The panel-maintained node counter is
        # the compatible 4.7.1 population denominator.
        node_population = int(r["total_users"] or 0)
        if node_population < int(thresholds["cluster_node_min_total_users"]):
            continue
        if affected / max(node_population, 1) < float(thresholds["cluster_node_min_share"]):
            continue
        clusters.append({
            "kind": "node",
            "key": str(r["node_uuid"]),
            "label": r["name"],
            "affected_users": affected,
            "total_users": node_population,
            "member_uuids": r["members"],
            "window_start": now - timedelta(minutes=node_window),
            "window_end": now,
        })

    # Do not infer an ISP outage from unrelated policy/abuse violations that
    # happen to share a large ASN. Provider outages use the independent,
    # bounded IODA signal; local mass incidents require reconnect evidence on
    # one of our nodes plus population/share thresholds above.
    return clusters
