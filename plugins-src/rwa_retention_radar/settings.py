"""Пороги сегментов: сколько дней тишины считать уходом и так далее.

Хранятся в настройках плагина (БД панели), переопределяются переменными
окружения с префиксом ``RETENTION_RADAR_``. Значения по умолчанию
подобраны под месячные подписки: неделя тишины — уже сигнал, две недели —
почти приговор.
"""
from __future__ import annotations

import os
from typing import Dict

KEY_THRESHOLDS = "thresholds"

# Имена совпадают с i18n-ключами панели
# (plugins.retention_radar.settings.fields.*) — переименовывать нельзя.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    # Сколько дней без единого выхода в сеть считать «тихо ушёл».
    "silent_days": 7.0,
    # Порог «глубокой» тишины — такие в списке помечаются красным.
    "silent_deep_days": 21.0,
    # За сколько дней до конца подписки начинать беспокоиться.
    "expiring_days": 7.0,
    # Сколько дней тишины перед истечением означают «продлевать не будет».
    "expiring_risk_days": 3.0,
    # Как долго после истечения человек ещё считается возвращаемым.
    "lapsed_days": 30.0,
    # Окно, в котором новичок обязан хотя бы раз подключиться.
    "onboarding_days": 7.0,
    # Как часто пересчитывать дневной срез (для графика динамики).
    "snapshot_recompute_seconds": 900.0,
    # Сколько дней динамики отдавать на дашборд.
    "trend_days": 14.0,
    # Скидка на кампанию по каждому сегменту, %. Логика простая: чем
    # дальше человек ушёл, тем дороже его возвращать. Истекающему хватает
    # напоминания, ушедшему месяц назад — уже нет.
    "discount_expiring": 15.0,
    "discount_silent": 20.0,
    "discount_lapsed": 30.0,
    "discount_stalled": 0.0,
    # Сколько часов живёт выданная скидка.
    "offer_valid_hours": 72.0,
    # Не писать одному человеку чаще, чем раз в N дней — независимо от
    # того, в скольких сегментах он оказался.
    "message_cooldown_days": 14.0,
    # Потолок получателей одной кампании. Защита от «разослал всей базе,
    # не глядя»: чтобы отправить больше, придётся сделать это осознанно.
    "max_recipients_per_campaign": 200.0,
    # Карантин вокруг автоматики Bedolaga. Бот сам напоминает о продлении
    # за 3 и 1 день, а после истечения ведёт лестницу скидок: день 1 —
    # уведомление, дни 2-3 — 10%, день 5 — 20%. Пока он работает, лезть
    # туда нельзя: человек получит два письма подряд, а наша скидка
    # перебьёт его воронку и отдаст больше, чем было нужно.
    # Значения = ширина окна автоматики + запас в сутки.
    "skip_days_before_expire": 4.0,
    "skip_days_after_expire": 6.0,
}

ENV_PREFIX = "RETENTION_RADAR_"


def _env_override(name: str) -> float | None:
    raw = os.environ.get(ENV_PREFIX + name.upper())
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def get_thresholds(settings) -> Dict[str, float]:
    stored = await settings.get(KEY_THRESHOLDS, None)
    values = dict(DEFAULT_THRESHOLDS)
    if isinstance(stored, dict):
        for key in DEFAULT_THRESHOLDS:
            if isinstance(stored.get(key), (int, float)):
                values[key] = float(stored[key])
    for key in DEFAULT_THRESHOLDS:
        env = _env_override(key)
        if env is not None:
            values[key] = env
    return values


async def patch_thresholds(settings, patch: Dict[str, float]) -> Dict[str, float]:
    stored = await settings.get(KEY_THRESHOLDS, {}) or {}
    for key, value in patch.items():
        if key not in DEFAULT_THRESHOLDS or not isinstance(value, (int, float)):
            continue
        if key.startswith("discount_"):
            # Ноль — осмысленное значение: кампания без скидки, одно
            # напоминание. Выше 100% скидки не бывает.
            stored[key] = max(0.0, min(100.0, float(value)))
        else:
            # Для окон и порогов ноль бессмыслен: сегмент либо схлопнется
            # в пустоту, либо соберёт вообще всех.
            stored[key] = max(1.0, float(value))
    await settings.set(KEY_THRESHOLDS, stored)
    return await get_thresholds(settings)


def get_bedolaga_config() -> Dict[str, str | None]:
    """Доступ к Bedolaga берём из окружения панели: там он уже настроен
    для встроенной интеграции, второй копии токена заводить незачем."""
    return {
        "base_url": os.environ.get("BEDOLAGA_API_URL") or None,
        "token": os.environ.get("BEDOLAGA_API_TOKEN") or None,
    }
