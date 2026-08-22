"""Настройки плагина: пороги правил и конфигурация ИИ-провайдера.

Всё лежит в общей таблице ``plugin_settings`` под ``plugin_id='smart_support'``
через фасад ``ctx.settings``. Конфиг провайдера дополнительно умеет читаться
из переменных окружения — чтобы поднять плагин с нуля, не дёргая API.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

# Ключи в plugin_settings
KEY_THRESHOLDS = "thresholds"
KEY_AI = "ai"
KEY_AI_PROVIDER = "ai_provider"
KEY_AI_SETTINGS = "ai_settings_v2"
KEY_CLIENT_VERSIONS = "client_latest_versions"

# 4.5.6 renders this complete list and allows the operator to reorder it.
# QCode uses the Anthropic-compatible route; Sonnet is intentionally first
# because that is the model selected for support diagnosis in this install.
AI_PROVIDER_NAMES = ("qcode", "gemini", "groq", "openrouter", "anthropic")
LEGACY_ONLY_PROVIDER_NAMES = (
    "qcode_openai",
    "qcode_gemini",
    "custom",
    "deepseek",
    "openai",
)
DEFAULT_PROVIDER_CHAIN = list(AI_PROVIDER_NAMES)
DEFAULT_PROVIDER_MODELS = {
    "qcode": "claude-sonnet-4-6",
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openai/gpt-4o-mini",
    "anthropic": "claude-sonnet-4-6",
}

# Пороги по умолчанию. Имена совпадают с ThresholdSettings и с i18n-ключами
# панели (plugins.smart_support.settings.fields.*) — переименовывать нельзя.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "node_cpu_high": 85.0,
    "node_cpu_critical": 95.0,
    "node_memory_high": 90.0,
    "node_metrics_stale_seconds": 1800.0,
    "traffic_high": 95.0,
    "traffic_full": 100.0,
    "traffic_high_confidence": 0.85,
    "traffic_full_confidence": 1.0,
    "cluster_node_window_minutes": 15.0,
    "cluster_node_reconnects_per_user": 3.0,
    "cluster_node_min_affected": 5.0,
    "cluster_asn_window_minutes": 60.0,
    "cluster_asn_min_affected": 5.0,
    "correlation_recompute_seconds": 300.0,
    "correlation_max_age_minutes": 60.0,
    # Не показываются отдельными контролами, но защищают от ложного
    # «массового инцидента» на маленькой выборке.
    "cluster_node_min_share": 0.4,
    "cluster_node_min_total_users": 10.0,
    # Утихшие корреляции остаются в отчёте как история, но правила их уже
    # не считают действующим инцидентом.
    "correlation_history_minutes": 180.0,
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
    modern = await settings.get(KEY_AI_SETTINGS, None)
    if isinstance(modern, dict) and "enabled" in modern:
        return bool(modern["enabled"])
    state = await settings.get(KEY_AI, None)
    if isinstance(state, dict) and "enabled" in state:
        return bool(state["enabled"])
    # По умолчанию ИИ включён, если ключ провайдера вообще настроен.
    cfg = await get_ai_provider(settings)
    return bool(cfg.get("api_key"))


async def set_ai_enabled(settings, enabled: bool) -> bool:
    modern = await settings.get(KEY_AI_SETTINGS, {}) or {}
    if not isinstance(modern, dict):
        modern = {}
    modern["enabled"] = bool(enabled)
    await settings.set(KEY_AI_SETTINGS, modern)
    state = await settings.get(KEY_AI, {}) or {}
    if not isinstance(state, dict):
        state = {}
    state["enabled"] = bool(enabled)
    await settings.set(KEY_AI, state)
    return bool(enabled)


def _env(name: str) -> Optional[str]:
    value = os.environ.get(ENV_PREFIX + name)
    return value.strip() if value and value.strip() else None


def _environment_ai_provider() -> Dict[str, Any]:
    api_key = _env("AI_API_KEY")
    return {
        "provider": _env("AI_PROVIDER") or "gemini",
        "model": _env("AI_MODEL"),
        "base_url": _env("AI_BASE_URL"),
        "proxy": _env("AI_PROXY"),
        "api_key": api_key,
        "monthly_limit": int(_env("AI_MONTHLY_LIMIT") or 0),
        "reply_language": _env("AI_REPLY_LANGUAGE") or DEFAULT_REPLY_LANGUAGE,
        "source": "env" if api_key else "none",
    }


async def _legacy_ai_provider(settings) -> Dict[str, Any]:
    """Конфиг провайдера: запись в БД важнее переменных окружения.

    Возвращает сырой словарь (включая api_key) — наружу его отдаёт только
    :func:`redact`, ключ в API не светится.
    """
    stored = await settings.get(KEY_AI_PROVIDER, None)
    if isinstance(stored, dict) and stored.get("api_key"):
        cfg = dict(stored)
        cfg.setdefault("source", "db")
        return cfg

    env_cfg = _environment_ai_provider()
    if not isinstance(stored, dict):
        return env_cfg

    stored_name = _normalise_provider(stored.get("provider"))
    env_name = _normalise_provider(env_cfg.get("provider"))
    same_provider = not stored_name or stored_name == env_name
    cfg = {
        "provider": stored.get("provider") or env_cfg["provider"],
        "model": stored.get("model") or (env_cfg.get("model") if same_provider else None),
        "base_url": stored.get("base_url") or (
            env_cfg.get("base_url") if same_provider else None
        ),
        "proxy": stored.get("proxy") or (env_cfg.get("proxy") if same_provider else None),
        "api_key": env_cfg.get("api_key") if same_provider else None,
        "monthly_limit": stored.get("monthly_limit", env_cfg.get("monthly_limit", 0)),
        "reply_language": stored.get("reply_language")
        or env_cfg.get("reply_language")
        or DEFAULT_REPLY_LANGUAGE,
        "source": env_cfg.get("source") if same_provider else "none",
    }
    return cfg


def _normalise_provider(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalise_chain(value: Any) -> List[str]:
    chain: List[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            provider = _normalise_provider(item)
            if provider in AI_PROVIDER_NAMES and provider not in chain:
                chain.append(provider)
    for provider in DEFAULT_PROVIDER_CHAIN:
        if provider not in chain:
            chain.append(provider)
    return chain


async def get_ai_settings(settings) -> Dict[str, Any]:
    """Resolved 4.5.6 settings contract, including secrets for local use.

    API handlers must pass the result through :func:`public_ai_settings`.
    The legacy single-provider record is merged read-only so upgrading does
    not disable an already working QCode/Sonnet setup.
    """
    stored = await settings.get(KEY_AI_SETTINGS, {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    raw_providers = stored.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raw_providers = {}

    providers: Dict[str, Dict[str, Any]] = {}
    for name in AI_PROVIDER_NAMES:
        item = raw_providers.get(name) or {}
        if not isinstance(item, dict):
            item = {}
        providers[name] = {
            "api_key": str(item.get("api_key") or "").strip() or None,
            "model": str(item.get("model") or "").strip()
            or DEFAULT_PROVIDER_MODELS[name],
            "base_url": str(item.get("base_url") or "").strip() or None,
            "proxy": str(item.get("proxy") or "").strip() or None,
        }

    # Environment secrets remain runtime-only. They participate in the chain,
    # but are never copied to plugin_settings merely because an operator edits
    # an unrelated modern setting.
    env_provider = _environment_ai_provider()
    env_name = _normalise_provider(env_provider.get("provider"))
    if env_name in providers and env_provider.get("api_key"):
        env_item = raw_providers.get(env_name) or {}
        env_key_cleared = isinstance(env_item, dict) and bool(env_item.get("key_cleared"))
        if not providers[env_name]["api_key"] and not env_key_cleared:
            providers[env_name]["api_key"] = env_provider["api_key"]
        if env_provider.get("model") and not (
            isinstance(env_item, dict) and env_item.get("model")
        ):
            providers[env_name]["model"] = env_provider["model"]
        for field in ("base_url", "proxy"):
            if env_provider.get(field) and not providers[env_name].get(field):
                providers[env_name][field] = env_provider[field]

    legacy = await _legacy_ai_provider(settings)
    legacy_name = _normalise_provider(legacy.get("provider"))
    if legacy_name in providers:
        legacy_item = raw_providers.get(legacy_name) or {}
        key_was_cleared = isinstance(legacy_item, dict) and bool(legacy_item.get("key_cleared"))
        if (
            not providers[legacy_name]["api_key"]
            and legacy.get("api_key")
            and not key_was_cleared
        ):
            providers[legacy_name]["api_key"] = legacy["api_key"]
        if legacy.get("model") and not legacy_item.get("model"):
            providers[legacy_name]["model"] = str(legacy["model"])
        for field in ("base_url", "proxy"):
            if legacy.get(field) and not legacy_item.get(field):
                providers[legacy_name][field] = str(legacy[field])

    chain = _normalise_chain(stored.get("provider_chain"))
    # A configured legacy provider remains first until the operator explicitly
    # stores a modern chain. New installs start with QCode/Sonnet.
    if "provider_chain" not in stored and legacy_name in chain and legacy.get("api_key"):
        chain.remove(legacy_name)
        chain.insert(0, legacy_name)

    enabled = (
        bool(stored["enabled"])
        if "enabled" in stored
        else await ai_enabled_legacy(settings, legacy)
    )
    return {
        "enabled": enabled,
        "provider_chain": chain,
        "providers": providers,
        "outage_lookup_enabled": bool(stored.get("outage_lookup_enabled", True)),
        "legacy": legacy,
    }


async def ai_enabled_legacy(settings, legacy: Optional[Dict[str, Any]] = None) -> bool:
    state = await settings.get(KEY_AI, None)
    if isinstance(state, dict) and "enabled" in state:
        return bool(state["enabled"])
    return bool((legacy or await _legacy_ai_provider(settings)).get("api_key"))


def public_ai_settings(resolved: Dict[str, Any]) -> Dict[str, Any]:
    """Secret-safe representation consumed by the 4.5.6 Settings page."""
    providers = resolved.get("providers") or {}
    return {
        "enabled": bool(resolved.get("enabled")),
        "provider_chain": list(resolved.get("provider_chain") or DEFAULT_PROVIDER_CHAIN),
        "providers": [
            {
                "provider": name,
                "key_set": bool((providers.get(name) or {}).get("api_key")),
                "model": (providers.get(name) or {}).get("model"),
            }
            for name in resolved.get("provider_chain") or DEFAULT_PROVIDER_CHAIN
        ],
        "outage_lookup_enabled": bool(resolved.get("outage_lookup_enabled")),
    }


async def get_ai_providers(settings) -> List[Dict[str, Any]]:
    """Configured providers in failover order, with legacy transport knobs."""
    resolved = await get_ai_settings(settings)
    legacy = resolved["legacy"]
    legacy_name = _normalise_provider(legacy.get("provider"))
    env_provider = _environment_ai_provider()
    env_name = _normalise_provider(env_provider.get("provider"))
    result: List[Dict[str, Any]] = []
    for name in resolved["provider_chain"]:
        item = resolved["providers"][name]
        cfg: Dict[str, Any] = {
            "provider": name,
            "api_key": item.get("api_key"),
            "model": item.get("model"),
            "monthly_limit": int(legacy.get("monthly_limit") or 0),
            "reply_language": legacy.get("reply_language") or DEFAULT_REPLY_LANGUAGE,
            "source": "db" if item.get("api_key") else "none",
        }
        for key in ("base_url", "proxy"):
            if item.get(key):
                cfg[key] = item[key]
        if name == legacy_name:
            for key in ("base_url", "proxy"):
                if legacy.get(key):
                    cfg[key] = legacy[key]
            if legacy.get("source") == "env" and item.get("api_key") == legacy.get("api_key"):
                cfg["source"] = "env"
        if name == env_name and item.get("api_key") == env_provider.get("api_key"):
            for key in ("base_url", "proxy"):
                if env_provider.get(key):
                    cfg[key] = env_provider[key]
            cfg["source"] = "env"
        result.append(cfg)
    # Keep old DB/env installations working without exposing legacy-only
    # providers in the new five-provider settings editor. An explicitly
    # configured modern provider takes precedence; otherwise the raw legacy
    # transport remains the effective fallback.
    if (
        not any(cfg.get("api_key") for cfg in result)
        and legacy_name in LEGACY_ONLY_PROVIDER_NAMES
        and legacy.get("api_key")
    ):
        legacy_cfg = dict(legacy)
        legacy_cfg["provider"] = legacy_name
        legacy_cfg.setdefault("source", "db")
        result.insert(0, legacy_cfg)
    return result


async def get_ai_provider(settings) -> Dict[str, Any]:
    """First configured provider, preserving the legacy public helper."""
    providers = await get_ai_providers(settings)
    configured = next((cfg for cfg in providers if cfg.get("api_key")), None)
    return configured or (providers[0] if providers else await _legacy_ai_provider(settings))


async def patch_ai_settings(settings, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a partial 4.5.6 patch without ever echoing provider secrets."""
    stored = await settings.get(KEY_AI_SETTINGS, {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    providers = stored.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}

    # Persist the working legacy provider before the first modern edit. Without
    # this migration, reordering QCode -> Gemini overwrote the legacy record,
    # lost the QCode key and sent the Gemini key to QCode's custom base URL.
    legacy_before = await _legacy_ai_provider(settings)
    legacy_name = _normalise_provider(legacy_before.get("provider"))
    if (
        legacy_name in AI_PROVIDER_NAMES
        and legacy_before.get("api_key")
        and legacy_before.get("source") != "env"
    ):
        legacy_item = providers.get(legacy_name) or {}
        if not isinstance(legacy_item, dict):
            legacy_item = {}
        if not legacy_item.get("api_key") and not legacy_item.get("key_cleared"):
            legacy_item["api_key"] = legacy_before["api_key"]
        if legacy_before.get("model") and not legacy_item.get("model"):
            legacy_item["model"] = legacy_before["model"]
        for field in ("base_url", "proxy"):
            if legacy_before.get(field) and not legacy_item.get(field):
                legacy_item[field] = legacy_before[field]
        providers[legacy_name] = legacy_item

    if patch.get("enabled") is not None:
        stored["enabled"] = bool(patch["enabled"])
        await set_ai_enabled(settings, bool(patch["enabled"]))
    if patch.get("outage_lookup_enabled") is not None:
        stored["outage_lookup_enabled"] = bool(patch["outage_lookup_enabled"])
    if patch.get("provider_chain") is not None:
        stored["provider_chain"] = _normalise_chain(patch["provider_chain"])

    for field, target in (("keys", "api_key"), ("models", "model")):
        values = patch.get(field)
        if not isinstance(values, dict):
            continue
        for raw_name, raw_value in values.items():
            name = _normalise_provider(raw_name)
            if name not in AI_PROVIDER_NAMES:
                continue
            item = providers.get(name) or {}
            if not isinstance(item, dict):
                item = {}
            value = str(raw_value or "").strip()
            if value:
                item[target] = value
                if target == "api_key":
                    item.pop("key_cleared", None)
            else:
                item.pop(target, None)
                if target == "api_key":
                    item["key_cleared"] = True
            providers[name] = item

    stored["providers"] = providers
    await settings.set(KEY_AI_SETTINGS, stored)

    # Explicit clearing must not be silently resurrected by the legacy record.
    key_patch = patch.get("keys")
    if isinstance(key_patch, dict):
        legacy = await settings.get(KEY_AI_PROVIDER, {}) or {}
        if isinstance(legacy, dict):
            legacy_name = _normalise_provider(legacy.get("provider"))
            if legacy_name in key_patch and not str(key_patch[legacy_name] or "").strip():
                legacy.pop("api_key", None)
                await settings.set(KEY_AI_PROVIDER, legacy)

    # Block Radar consumes get_ai_provider(), which already resolves the modern
    # chain. Do not rewrite the legacy row here: its base_url/proxy belong to
    # its original provider and must never follow a different key.
    resolved = await get_ai_settings(settings)
    return resolved


async def patch_ai_provider(settings, patch: Dict[str, Any]) -> Dict[str, Any]:
    stored = await settings.get(KEY_AI_PROVIDER, {}) or {}
    if not isinstance(stored, dict):
        stored = {}
    original = dict(stored)
    original_name = _normalise_provider(original.get("provider"))
    if original_name in AI_PROVIDER_NAMES and original.get("api_key"):
        modern = await settings.get(KEY_AI_SETTINGS, {}) or {}
        if not isinstance(modern, dict):
            modern = {}
        providers = modern.get("providers") or {}
        if not isinstance(providers, dict):
            providers = {}
        item = providers.get(original_name) or {}
        if not isinstance(item, dict):
            item = {}
        if not item.get("api_key") and not item.get("key_cleared"):
            item["api_key"] = original["api_key"]
        if original.get("model") and not item.get("model"):
            item["model"] = original["model"]
        for field in ("base_url", "proxy"):
            if original.get(field) and not item.get(field):
                item[field] = original[field]
        providers[original_name] = item
        modern["providers"] = providers
        await settings.set(KEY_AI_SETTINGS, modern)

    requested_name = _normalise_provider(patch.get("provider"))
    provider_changed = bool(requested_name and requested_name != original_name)
    transport_changed = any(
        field in patch and str(patch.get(field) or "").strip() != str(original.get(field) or "").strip()
        for field in ("base_url", "proxy")
    )
    if provider_changed:
        # Transport overrides and model names are provider-specific. Carrying
        # a QCode relay URL/proxy into Gemini would send the new provider's key
        # to the old endpoint. Reset omitted fields on a provider switch.
        for field in ("base_url", "proxy"):
            if field not in patch:
                stored.pop(field, None)
        if "model" not in patch and requested_name in DEFAULT_PROVIDER_MODELS:
            stored["model"] = DEFAULT_PROVIDER_MODELS[requested_name]
    if (provider_changed or transport_changed) and "api_key" not in patch:
        # Changing the destination while retaining a hidden secret turns edit
        # access into key exfiltration. Require the operator to re-enter a key
        # whenever provider transport changes.
        stored.pop("api_key", None)
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
    provider = _normalise_provider(stored.get("provider"))
    if provider in AI_PROVIDER_NAMES:
        modern_patch: Dict[str, Any] = {"provider_chain": [provider]}
        if "api_key" in patch:
            modern_patch["keys"] = {provider: stored.get("api_key", "")}
        elif transport_changed:
            # A stored modern key would otherwise be paired with the newly
            # supplied legacy destination. Clear it until the operator proves
            # possession by supplying the key in the same request.
            modern_patch["keys"] = {provider: ""}
        if "model" in patch:
            modern_patch["models"] = {provider: stored.get("model", "")}
        await patch_ai_settings(settings, modern_patch)
    return await get_ai_provider(settings)


def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Публичный вид конфига — без ключа."""
    def safe_url(value: Any) -> Optional[str]:
        if not value:
            return None
        try:
            parsed = urlsplit(str(value))
            host = parsed.hostname or ""
            if not parsed.scheme or not host:
                return None
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{host}:{parsed.port}" if parsed.port else host
            # Credentials, path segments, query strings and fragments may all
            # carry tokens. A read-only viewer only needs the safe origin.
            return urlunsplit((parsed.scheme, netloc, "", "", ""))
        except (TypeError, ValueError):
            return None

    return {
        "provider": cfg.get("provider") or "gemini",
        "model": cfg.get("model"),
        "base_url": safe_url(cfg.get("base_url")),
        "proxy": safe_url(cfg.get("proxy")),
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
