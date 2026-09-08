"""Кампании: от сегмента до отправленного сообщения.

Порядок жёсткий и одинаковый всегда:

1. ``preview`` — считаем получателей, генерируем текст, возвращаем оператору
   вместе с ``confirm_token``;
2. оператор читает и правит текст;
3. ``send`` — принимает токен, посчитанный от того, что оператор видел.
   Если текст, сегмент, скидка или состав получателей изменились, токен не
   сойдётся и отправка не состоится.

Так исключается главный риск: разослать живым людям не то, что человек
проверил. По той же причине здесь нет и не должно быть автоотправки по
расписанию — сообщение уходит только после явного действия оператора.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from collections.abc import Collection, Iterable
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import ai, bedolaga, data, store
from rwa_incident_hub import active_node_uuids

# Ключ оффера в Bedolaga. Свой на каждый сегмент: повторная кампания по
# тому же сегменту обновит неиспользованную скидку, а не выдаст вторую.
OFFER_TYPE_PREFIX = "retention_radar_"

# Текст кнопки под сообщением. Разный по сегментам: «вернуться» тому, у
# кого подписка ещё активна, звучит странно, а «подключиться» уместно
# только для тех, кто ни разу не заходил.
BUTTON_LABELS = {
    "silent": "🎁 Забрать скидку",
    "expiring": "💚 Продлить со скидкой",
    "lapsed": "🎁 Вернуться со скидкой",
    "stalled": "🚀 Подключиться",
}
DEFAULT_BUTTON_LABEL = "🎁 Вернуться со скидкой"


def _parse_panel_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort ISO parser for untrusted JSON cached from the panel."""
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
    parsed = _parse_panel_timestamp(value)
    if parsed is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    return -30.0 <= age_seconds <= max(1, int(window_minutes)) * 60.0

# Куда ведёт кнопка: кабинет как Mini App. Параметр включает
# автоприменение — кабинет сам найдёт активный оффер человека, применит
# скидку и откроет тарифы.
CLAIM_QUERY = "claim_offer=auto"


def button_label(segment: str) -> str:
    return BUTTON_LABELS.get(segment, DEFAULT_BUTTON_LABEL)


def button_url() -> Optional[str]:
    """Ссылка на мини-апп. Без неё кнопку не рисуем вовсе: кнопка,
    ведущая в никуда, хуже, чем её отсутствие."""
    base = (os.environ.get("RETENTION_RADAR_MINIAPP_URL") or "").strip().rstrip("/")
    if not base:
        return None
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{CLAIM_QUERY}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def confirm_token(
    segment: str,
    message_text: str,
    discount_percent: int,
    telegram_ids: List[int],
    visible_user_uuids: Collection[str] | None = None,
) -> str:
    """Отпечаток текста, аудитории и границы видимости оператора.

    For unrestricted admins (``None``) the historical token format is kept
    byte-for-byte.  Scoped requests append a non-reversible digest of the
    complete whitelist, so even a scope change that does not alter today's
    segment invalidates the preview.
    """
    parts = [
        segment,
        message_text.strip(),
        str(discount_percent),
        ",".join(str(i) for i in sorted(telegram_ids)),
    ]
    if visible_user_uuids is not None:
        canonical_scope = ",".join(
            sorted({str(value).strip().lower() for value in visible_user_uuids})
        )
        scope_digest = hashlib.sha256(canonical_scope.encode("utf-8")).hexdigest()
        parts.append(f"scope:{scope_digest}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _in_bedolaga_window(user: Dict[str, Any], th: Dict[str, float]) -> bool:
    """Человек сейчас в работе у автоматики Bedolaga.

    Бот сам ведёт его от «за 3 дня до конца» до «5-й день после
    истечения». В этом окне наша кампания дала бы второе письмо в тот же
    день и подменила бы его лестницу скидок нашей, более щедрой.
    """
    days_left = user.get("days_until_expire")
    if days_left is None:
        return False
    if 0 <= days_left <= int(th["skip_days_before_expire"]):
        return True
    # Отрицательное значение = подписка уже истекла столько дней назад.
    if -int(th["skip_days_after_expire"]) <= days_left < 0:
        return True
    return False


async def _incident_affected_users(
    db, safety: Dict[str, Any], user_uuids: Iterable[str]
) -> set[str]:
    if not safety.get("suppress_active_incidents"):
        return set()
    values = sorted({str(value) for value in user_uuids if value})
    if not values:
        return set()
    # Incident Center omits explicitly reviewed false positives. Do not use
    # policy-violation history (including annulled signals) as outage evidence.
    nodes = await active_node_uuids(db)
    if not nodes:
        return set()
    rows = await db.fetch(
        """SELECT u.uuid::text AS user_uuid,
                  EXISTS (
                      SELECT 1
                        FROM user_connections c
                       WHERE c.user_uuid = u.uuid
                         AND c.node_uuid = ANY($1::uuid[])
                          AND c.connected_at >=
                              NOW() - make_interval(mins => $2)
                          AND c.connected_at <= NOW() + INTERVAL '30 seconds'
                   ) AS recent_connection,
                  u.raw_data -> 'userTraffic' ->> 'onlineAt' AS online_at,
                  u.raw_data -> 'userTraffic' ->> 'lastConnectedNodeUuid'
                      AS live_node_uuid
             FROM users u
            WHERE u.uuid = ANY($3::uuid[])""",
        sorted(nodes),
        int(safety["incident_lookback_minutes"]),
        values,
    )
    active_nodes = {str(node).lower() for node in nodes}
    window_minutes = int(safety["incident_lookback_minutes"])
    return {
        str(row["user_uuid"])
        for row in rows
        if bool(row.get("recent_connection"))
        or (
            str(row.get("live_node_uuid") or "").lower() in active_nodes
            and _recent_panel_activity(row.get("online_at"), window_minutes)
        )
    }


async def _active_throttled_users(
    db, user_uuids: Iterable[str]
) -> set[str]:
    """Resolve active soft-throttles without requiring migration 0102 yet."""
    values = sorted({str(value) for value in user_uuids if value})
    if not values:
        return set()
    exists = await db.fetchval("SELECT to_regclass($1)", "public.user_throttles")
    if not exists:
        return set()
    rows = await db.fetch(
        """SELECT user_uuid::text AS user_uuid
             FROM user_throttles
            WHERE user_uuid = ANY($1::uuid[])
              AND (until IS NULL OR until > NOW())""",
        values,
    )
    return {str(row["user_uuid"]) for row in rows}


async def recipients(
    db,
    segment: str,
    th: Dict[str, float],
    safety: Optional[Dict[str, Any]] = None,
    visible_user_uuids: Collection[str] | None = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Кому реально можно писать — и почему остальные отсеялись.

    Причины отсева показываем оператору: «в сегменте 90, писать будем 61» без
    объяснения выглядит как потеря данных.
    """
    everyone = await data.list_segment(
        db,
        segment,
        th,
        limit=5000,
        offset=0,
        visible_user_uuids=visible_user_uuids,
    )
    cooldown_days = int(th["message_cooldown_days"])
    limit = int(th["max_recipients_per_campaign"])

    recent = await store.recently_messaged(db, cooldown_days)
    affected = await _incident_affected_users(
        db, safety or {}, (user["uuid"] for user in everyone)
    ) if safety else set()
    throttled = await _active_throttled_users(db, (user["uuid"] for user in everyone))
    skipped = {
        "no_telegram": 0,
        "cooldown": 0,
        "over_limit": 0,
        "bedolaga_auto": 0,
        "active_incident": 0,
        "throttled": 0,
    }

    allowed: List[Dict[str, Any]] = []
    for user in everyone:
        # An active throttle is an administrative measure. Marketing must not
        # undermine it, whether this is a preview, dry-run, or live campaign.
        if user["uuid"] in throttled:
            skipped["throttled"] += 1
            continue
        if not user.get("telegram_id"):
            skipped["no_telegram"] += 1
            continue
        if _in_bedolaga_window(user, th):
            skipped["bedolaga_auto"] += 1
            continue
        if user["uuid"] in recent:
            skipped["cooldown"] += 1
            continue
        if user["uuid"] in affected:
            skipped["active_incident"] += 1
            continue
        allowed.append(user)

    if len(allowed) > limit:
        skipped["over_limit"] = len(allowed) - limit
        allowed = allowed[:limit]

    return allowed, skipped


async def preview(
    db,
    ctx,
    segment: str,
    th: Dict[str, float],
    custom_text: Optional[str] = None,
    safety: Optional[Dict[str, Any]] = None,
    visible_user_uuids: Collection[str] | None = None,
) -> Dict[str, Any]:
    people, skipped = await recipients(
        db, segment, th, safety, visible_user_uuids=visible_user_uuids
    )
    discount = int(th[f"discount_{segment}"])
    valid_hours = int(th["offer_valid_hours"])

    label = button_label(segment)
    link = button_url()
    ai_error: Optional[str] = None
    message = (custom_text or "").strip()
    if not message:
        try:
            message = await ai.generate_message(
                ai.get_config(), segment, discount, valid_hours, len(people), label
            )
        except ai.AIError as exc:
            # Без текста кампанию всё равно можно собрать руками —
            # возвращаем причину и пустое поле, а не 500.
            ai_error = str(exc)
            ctx.logger.warning("retention_radar.ai_failed", extra={"reason": str(exc)})

    telegram_ids = [int(u["telegram_id"]) for u in people]
    return {
        "segment": segment,
        "recipients": len(people),
        "skipped": skipped,
        "discount_percent": discount,
        "offer_valid_hours": valid_hours,
        "message_text": message,
        "button_label": label if link else "",
        "button_url": link,
        "ai_error": ai_error,
        # Проверяем ЛЮБОЙ текст, включая правку оператора: обещание
        # «пришлём ссылку» одинаково вредно, кто бы его ни написал.
        "warnings": ai.find_risky(message),
        "sample": people[:5],
        "confirm_token": confirm_token(
            segment,
            message,
            discount,
            telegram_ids,
            visible_user_uuids,
        ),
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def arm(
    db,
    *,
    confirm_token: str,
    admin_username: Optional[str],
    ttl_minutes: int,
    admin_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    raw_token = secrets.token_urlsafe(32)
    idempotency_key = str(uuid.uuid4())
    await store.issue_arm(
        db,
        token_hash=_token_hash(raw_token),
        confirm_token=confirm_token,
        idempotency_key=idempotency_key,
        admin_account_id=admin_account_id,
        admin_username=admin_username,
        ttl_minutes=ttl_minutes,
    )
    return {
        "arm_token": raw_token,
        "idempotency_key": idempotency_key,
        "expires_in_seconds": int(ttl_minutes) * 60,
    }


def _existing_result(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "campaign_id": int(row["id"]),
        "dry_run": bool(row["dry_run"]),
        "recipients": int(row["recipients"]),
        "offers_created": int(row["sent"] or 0),
        "offer_errors": int(row["failed"] or 0),
        "broadcast_id": row["broadcast_id"],
        "status": row["status"],
        # Historical rows predate the throttle exclusion counter.
        "skipped_throttled": 0,
    }


async def send(
    db,
    ctx,
    *,
    segment: str,
    message_text: str,
    token: str,
    dry_run: bool,
    admin_username: Optional[str],
    th: Dict[str, float],
    bedolaga_cfg: Dict[str, Any],
    safety: Dict[str, Any],
    arm_token: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    admin_account_id: Optional[int] = None,
    visible_user_uuids: Collection[str] | None = None,
) -> Dict[str, Any]:
    # Preserve the legacy fast idempotent retry for unrestricted admins.  A
    # scoped admin must resolve and re-bind the current audience first so a
    # policy change cannot replay an older preview.
    if not dry_run and idempotency_key and visible_user_uuids is None:
        existing = await store.campaign_by_idempotency(db, idempotency_key)
        if existing:
            return _existing_result(existing)

    people, skipped = await recipients(
        db,
        segment,
        th,
        safety,
        visible_user_uuids=visible_user_uuids,
    )
    discount = int(th[f"discount_{segment}"])
    valid_hours = int(th["offer_valid_hours"])
    telegram_ids = [int(u["telegram_id"]) for u in people]

    expected = confirm_token(
        segment,
        message_text,
        discount,
        telegram_ids,
        visible_user_uuids,
    )
    if token != expected:
        # Между предпросмотром и отправкой состав получателей мог измениться
        # (кто-то продлил подписку). Это не ошибка оператора — просто нужен
        # свежий предпросмотр.
        raise ValueError("stale_confirmation")

    if not dry_run and idempotency_key and visible_user_uuids is not None:
        existing = await store.campaign_by_idempotency(db, idempotency_key)
        if existing:
            return _existing_result(existing)

    if not people:
        raise ValueError("empty_audience")

    if not dry_run:
        if not safety.get("live_campaigns_enabled"):
            raise ValueError("live_campaigns_disabled")
        if safety.get("require_server_arm"):
            if not arm_token or not idempotency_key:
                raise ValueError("campaign_not_armed")
            consumed = await store.consume_arm(
                db,
                token_hash=_token_hash(arm_token),
                confirm_token=token,
                idempotency_key=idempotency_key,
                admin_account_id=admin_account_id,
                admin_username=admin_username,
            )
            if not consumed:
                raise ValueError("arm_invalid_or_expired")

    campaign_id, created = await store.open_campaign(
        db,
        segment=segment,
        discount_percent=discount,
        valid_hours=valid_hours,
        message_text=message_text,
        recipients=len(people),
        dry_run=dry_run,
        admin_username=admin_username,
        idempotency_key=idempotency_key if not dry_run else None,
    )
    if not created:
        existing = await store.campaign_by_idempotency(db, idempotency_key)
        if existing:
            return _existing_result(existing)

    if dry_run:
        await store.close_campaign(
            db, campaign_id, status="dry_run", sent=0, failed=0, broadcast_id=None
        )
        ctx.logger.info(
            "retention_radar.campaign_dry_run",
            extra={"segment": segment, "recipients": len(people)},
        )
        return {
            "campaign_id": campaign_id,
            "dry_run": True,
            "recipients": len(people),
            "offers_created": 0,
            "offer_errors": 0,
            "broadcast_id": None,
            "status": "dry_run",
            "skipped_throttled": int(skipped.get("throttled", 0)),
        }

    # 1. Персональные скидки. Падение на одном человеке не должно ронять
    # кампанию: остальным оффер всё равно нужен.
    offers_created = 0
    offer_errors = 0
    for user in people:
        try:
            await bedolaga.create_offer(
                bedolaga_cfg,
                telegram_id=int(user["telegram_id"]),
                discount_percent=discount,
                valid_hours=valid_hours,
                notification_type=f"{OFFER_TYPE_PREFIX}{segment}",
            )
            offers_created += 1
        except bedolaga.BedolagaError as exc:
            offer_errors += 1
            ctx.logger.warning(
                "retention_radar.offer_failed",
                extra={"telegram_id": user["telegram_id"], "reason": str(exc)},
            )

    # 2. Текст — одной рассылкой ровно этому списку.
    result = await bedolaga.send_broadcast(
        bedolaga_cfg,
        segment=segment,
        message_text=message_text,
        telegram_ids=telegram_ids,
        button_label=button_label(segment),
        button_url=button_url(),
    )
    broadcast_id = int(result.get("id") or 0) or None

    await store.record_sends(db, campaign_id, people)
    await store.close_campaign(
        db,
        campaign_id,
        status="sent",
        sent=offers_created,
        failed=offer_errors,
        broadcast_id=broadcast_id,
    )
    ctx.logger.info(
        "retention_radar.campaign_sent",
        extra={
            "segment": segment,
            "recipients": len(people),
            "offers": offers_created,
            "broadcast_id": broadcast_id,
        },
    )

    return {
        "campaign_id": campaign_id,
        "dry_run": False,
        "recipients": len(people),
        "offers_created": offers_created,
        "offer_errors": offer_errors,
        "broadcast_id": broadcast_id,
        "status": "sent",
        "skipped_throttled": int(skipped.get("throttled", 0)),
    }
