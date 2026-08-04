"""Настройки плагина: пороги правил и конфигурация ИИ-провайдера.

Всё лежит в общей таблице ``plugin_settings`` под ``plugin_id='smart_support'``
через фасад ``ctx.settings``. Конфиг провайдера дополнительно умеет читаться
из переменных окружения — чтобы поднять плагин с нуля, не дёргая API.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Ключи в plugin_settings
KEY_THRESHOLDS = "thresholds"
KEY_AI = "ai"
KEY_AI_PROVIDER = "ai_provider"
KEY_CLIENT_VERSIONS = "client_latest_versions"

# Пороги по умолчанию. Имена совпадают с ThresholdSettings и с i18n-ключами
# панели (plugins.smart_support.settings.fields.*) — переименовывать нельзя.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "node_cpu_high": 85.0,
    "node_cpu_critical": 95.0,
    "node_memory_high": 90.0,
    "node_metrics_stale_seconds": 300.0,
    "traffic_high": 90.0,
    "traffic_full": 100.0,
    "traffic_high_confidence": 0.6,
    "traffic_full_confidence": 0.95,
    "cluster_node_window_minutes": 30.0,
    "cluster_node_reconnects_per_user": 4.0,
    "cluster_node_min_affected": 3.0,
    "cluster_asn_window_minutes": 60.0,
    "cluster_asn_min_affected": 3.0,
    "correlation_recompute_seconds": 300.0,
    "correlation_max_age_minutes": 60.0,
}

ENV_PREFIX = "SMART_SUPPORT_"

# Язык черновика ответа клиенту. Название языка словом, а не код: оно
# уходит прямо в промпт («Пиши на языке русский»).
DEFAULT_REPLY_LANGUAGE = "русский"


async def get_thresholds(settings) -> Dict[str, float]:
    """Дефолты, поверх которых легли переопределения оператора."""
    stored = await settings.get(KEY_THRESHOLDS, {}) or {}
    resolved = dict(DEFAULT_THRESHOLDS)
    for key, value in stored.items():
        if key in DEFAULT_THRESHOLDS and isinstance(value, (int, float)):
            resolved[key] = float(value)
    return resolved


async def patch_thresholds(settings, patch: Dict[str, Any]) -> Dict[str, float]:
    """Записать только пришедшие ключи, вернуть разрешённый набор."""
    stored = await settings.get(KEY_THRESHOLDS, {}) or {}
    for key, value in patch.items():
        if key not in DEFAULT_THRESHOLDS or value is None:
            continue
        try:
            stored[key] = float(value)
        except (TypeError, ValueError):
            continue
    await settings.set(KEY_THRESHOLDS, stored)
    return await get_thresholds(settings)


async def ai_enabled(settings) -> bool:
    state = await settings.get(KEY_AI, None)
    if isinstance(state, dict) and "enabled" in state:
        return bool(state["enabled"])
    # По умолчанию ИИ включён, если ключ провайдера вообще настроен.
    cfg = await get_ai_provider(settings)
    return bool(cfg.get("api_key"))


async def set_ai_enabled(settings, enabled: bool) -> bool:
    state = await settings.get(KEY_AI, {}) or {}
    state["enabled"] = bool(enabled)
    await settings.set(KEY_AI, state)
    return bool(enabled)


def _env(name: str) -> Optional[str]:
    value = os.environ.get(ENV_PREFIX + name)
    return value.strip() if value and value.strip() else None


async def get_ai_provider(settings) -> Dict[str, Any]:
    """Конфиг провайдера: запись в БД важнее переменных окружения.

    Возвращает сырой словарь (включая api_key) — наружу его отдаёт только
    :func:`redact`, ключ в API не светится.
    """
    stored = await settings.get(KEY_AI_PROVIDER, None)
    if isinstance(stored, dict) and stored.get("api_key"):
        cfg = dict(stored)
        cfg.setdefault("source", "db")
        return cfg

    api_key = _env("AI_API_KEY")
    cfg = {
        "provider": _env("AI_PROVIDER") or "gemini",
        "model": _env("AI_MODEL"),
        "base_url": _env("AI_BASE_URL"),
        "proxy": _env("AI_PROXY"),
        "api_key": api_key,
        "monthly_limit": int(_env("AI_MONTHLY_LIMIT") or 0),
        "reply_language": _env("AI_REPLY_LANGUAGE") or DEFAULT_REPLY_LANGUAGE,
        "source": "env" if api_key else "none",
    }
    # Частичное переопределение из БД без ключа (например, только модель).
    if isinstance(stored, dict):
        for key in ("provider", "model", "base_url", "proxy", "monthly_limit",
                    "reply_language"):
            if stored.get(key):
                cfg[key] = stored[key]
    return cfg


async def patch_ai_provider(settings, patch: Dict[str, Any]) -> Dict[str, Any]:
    stored = await settings.get(KEY_AI_PROVIDER, {}) or {}
    for key in ("provider", "model", "base_url", "proxy", "api_key", "reply_language"):
        if patch.get(key) is not None:
            value = str(patch[key]).strip()
            # Пустая строка — способ сбросить поле обратно к env/дефолту.
            if value:
                stored[key] = value
            else:
                stored.pop(key, None)
    if patch.get("monthly_limit") is not None:
        stored["monthly_limit"] = max(0, int(patch["monthly_limit"]))
    await settings.set(KEY_AI_PROVIDER, stored)
    return await get_ai_provider(settings)


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Публичный вид конфига — без ключа."""
    return {
        "provider": cfg.get("provider") or "gemini",
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "proxy": cfg.get("proxy"),
        "has_api_key": bool(cfg.get("api_key")),
        "monthly_limit": int(cfg.get("monthly_limit") or 0),
        "source": cfg.get("source") or "none",
    }


async def get_client_versions(settings) -> Dict[str, str]:
    """Справочник «последняя версия клиента» для правила client_version_outdated.

    Пусто по умолчанию: без него правило просто не срабатывает, и это
    честнее, чем пугать оператора выдуманной «устаревшей» версией.
    """
    stored = await settings.get(KEY_CLIENT_VERSIONS, {}) or {}
    return {str(k).lower(): str(v) for k, v in stored.items() if v}
