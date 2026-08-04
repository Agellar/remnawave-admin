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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import ai, bedolaga, data, store

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
    segment: str, message_text: str, discount_percent: int, telegram_ids: List[int]
) -> str:
    """Отпечаток того, что оператор видел в предпросмотре."""
    payload = "|".join(
        [
            segment,
            message_text.strip(),
            str(discount_percent),
            ",".join(str(i) for i in sorted(telegram_ids)),
        ]
    )
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


async def recipients(
    db, segment: str, th: Dict[str, float]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Кому реально можно писать — и почему остальные отсеялись.

    Причины отсева показываем оператору: «в сегменте 90, писать будем 61» без
    объяснения выглядит как потеря данных.
    """
    everyone = await data.list_segment(db, segment, th, limit=5000, offset=0)
    cooldown_days = int(th["message_cooldown_days"])
    limit = int(th["max_recipients_per_campaign"])

    recent = await store.recently_messaged(db, cooldown_days)
    skipped = {"no_telegram": 0, "cooldown": 0, "over_limit": 0, "bedolaga_auto": 0}

    allowed: List[Dict[str, Any]] = []
    for user in everyone:
        if not user.get("telegram_id"):
            skipped["no_telegram"] += 1
            continue
        if _in_bedolaga_window(user, th):
            skipped["bedolaga_auto"] += 1
            continue
        if user["uuid"] in recent:
            skipped["cooldown"] += 1
            continue
        allowed.append(user)

    if len(allowed) > limit:
        skipped["over_limit"] = len(allowed) - limit
        allowed = allowed[:limit]

    return allowed, skipped


async def preview(
    db, ctx, segment: str, th: Dict[str, float], custom_text: Optional[str] = None
) -> Dict[str, Any]:
    people, skipped = await recipients(db, segment, th)
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
        "confirm_token": confirm_token(segment, message, discount, telegram_ids),
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
) -> Dict[str, Any]:
    people, _ = await recipients(db, segment, th)
    discount = int(th[f"discount_{segment}"])
    valid_hours = int(th["offer_valid_hours"])
    telegram_ids = [int(u["telegram_id"]) for u in people]

    expected = confirm_token(segment, message_text, discount, telegram_ids)
    if token != expected:
        # Между предпросмотром и отправкой состав получателей мог измениться
        # (кто-то продлил подписку). Это не ошибка оператора — просто нужен
        # свежий предпросмотр.
        raise ValueError("stale_confirmation")

    if not people:
        raise ValueError("empty_audience")

    campaign_id = await store.open_campaign(
        db,
        segment=segment,
        discount_percent=discount,
        valid_hours=valid_hours,
        message_text=message_text,
        recipients=len(people),
        dry_run=dry_run,
        admin_username=admin_username,
    )

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
            "broadcast_id": None,
            "status": "dry_run",
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
    }
