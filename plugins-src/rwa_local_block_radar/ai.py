"""Sonnet incident analysis for Local Block Radar.

The deterministic detector remains authoritative. Sonnet receives only
infrastructure aggregates and explains what type of incident best fits them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from rwa_smart_support.settings import get_ai_provider

from . import settings as settings_mod, store


SONNET_MODEL = "claude-sonnet-4-6"
DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "qcode": "https://api.qcode.cc/api",
}
ALLOWED_PROVIDERS = {"anthropic", "qcode"}
CLASSIFICATIONS = {
    "likely_block",
    "provider_outage",
    "node_failure",
    "traffic_shift",
    "insufficient_data",
}


SYSTEM_PROMPT = """Ты — сетевой инженер-аналитик Local Block Radar для VPN-инфраструктуры Remnawave.

Цель анализа: по агрегированным измерениям определить наиболее вероятный ТИП подтверждённой просадки конкретной ноды/транспорта. Детерминированный радар уже обнаружил статистическое отклонение; ты не решаешь, создавать или закрывать инцидент, не меняешь конфигурацию и не инициируешь уведомления.

Допустимые классификации — выбери ровно одну:
1. likely_block — нода остаётся доступной, но её доля и онлайн устойчиво и избирательно падают относительно baseline, тогда как остальная сеть сравнительно стабильна. Это гипотеза о сетевой фильтрации/недоступности транспорта, а не доказанный факт блокировки.
2. provider_outage — видны признаки сбоя площадки или провайдера: нода недоступна либо одновременно деградируют связанные инфраструктурные направления, независимо от конкретного транспорта.
3. node_failure — проблема локальна для ноды/агента/сервиса: node_alive=false, обрыв телеметрии или полный локальный провал без данных об избирательности транспорта.
4. traffic_shift — снижение объяснимо общим изменением онлайна, сезонностью, перераспределением пользователей/маршрутов или ростом других нод; доказательств аварии или фильтрации недостаточно.
5. insufficient_data — наблюдений мало, они противоречивы или одна панель не позволяет надёжно отделить причины.

Правила доказательности:
- Оценивай и абсолютный online, и долю share. Падение только одного показателя слабее согласованного падения обоих.
- Сравни последние точки во времени с baseline и с network_latest. Разовая точка не подтверждает устойчивый эффект.
- node_alive=false прежде всего указывает на node_failure/provider_outage, а не на блокировку.
- Если вся сеть падает одновременно, это не избирательная блокировка рассматриваемого транспорта.
- Одна локальная панель не доказывает массовую или государственную блокировку. Без независимых наблюдателей не ставь likely_block с уверенностью выше 0.70.
- Не придумывай страны, операторов, ASN, причины, внешние события и результаты проверок, которых нет во входных данных.
- Не анализируй отдельных пользователей: во входе намеренно нет IP, UUID пользователей, Telegram ID или истории конкретного человека.
- confidence — число от 0 до 1, отражающее качество доказательств, а не серьёзность события.

Рекомендации должны быть безопасными и проверяемыми. Сначала read-only проверки: состояние ноды/агента, сравнение транспортов и соседних нод, внешние пробы из нескольких сетей, логи без секретов. Не предлагай автоматически перезапускать сервисы, менять маршрутизацию, удалять данные или рассылать сообщения.

support_note — нейтральная короткая формулировка для поддержки. Не заявляй о подтверждённой блокировке, если классификация и данные этого не доказывают. Не обещай срок исправления.

Верни результат только через предоставленный инструмент и строго по его схеме. Пиши summary, evidence, recommendations и support_note на русском языке."""


ANALYSIS_TOOL = {
    "name": "record_radar_analysis",
    "description": "Record a calibrated infrastructure-only analysis of a confirmed radar dip.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "classification": {"type": "string", "enum": sorted(CLASSIFICATIONS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "summary": {"type": "string", "minLength": 1, "maxLength": 700},
            "evidence": {
                "type": "array",
                "maxItems": 6,
                "items": {"type": "string", "maxLength": 300},
            },
            "recommendations": {
                "type": "array",
                "maxItems": 6,
                "items": {"type": "string", "maxLength": 300},
            },
            "support_note": {"type": "string", "maxLength": 500},
        },
        "required": [
            "classification",
            "confidence",
            "summary",
            "evidence",
            "recommendations",
            "support_note",
        ],
    },
}


class AIError(Exception):
    pass


def _public(row: dict) -> dict:
    def array(value: Any) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return [str(item) for item in (value or [])]

    return {
        "id": int(row["id"]),
        "alert_id": int(row["alert_id"]),
        "classification": row["classification"],
        "confidence": float(row["confidence"]),
        "summary": row["summary"],
        "evidence": array(row["evidence"]),
        "recommendations": array(row["recommendations"]),
        "support_note": row.get("support_note"),
        "provider": row["provider"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _clean_list(value: Any, *, limit: int = 6, chars: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:chars] for item in value[:limit] if str(item).strip()]


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    classification = str(value.get("classification") or "")
    if classification not in CLASSIFICATIONS:
        classification = "insufficient_data"
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    # A single local panel cannot justify high-confidence blocking attribution.
    if classification == "likely_block":
        confidence = min(confidence, 0.70)
    summary = str(value.get("summary") or "").strip()[:700]
    if not summary:
        raise AIError("empty_summary")
    return {
        "classification": classification,
        "confidence": confidence,
        "summary": summary,
        "evidence": _clean_list(value.get("evidence")),
        "recommendations": _clean_list(value.get("recommendations")),
        "support_note": str(value.get("support_note") or "").strip()[:500] or None,
    }


async def provider_status(settings, db) -> dict:
    radar_cfg = await settings_mod.get(settings)
    shared = await get_ai_provider(settings)
    provider = str(shared.get("provider") or "").lower().replace("-", "_")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "enabled": bool(radar_cfg["ai_enabled"]),
        "auto_analyze": bool(radar_cfg["ai_auto_analyze"]),
        "configured": bool(shared.get("api_key")) and provider in ALLOWED_PROVIDERS,
        "provider": provider or None,
        "model": radar_cfg["ai_model"],
        "used": await store.ai_usage(db, period),
        "monthly_limit": int(radar_cfg["ai_monthly_limit"]),
    }


async def analyze_alert(ctx, alert_id: int, *, force: bool = False) -> dict:
    radar_cfg = await settings_mod.get(ctx.settings)
    if not radar_cfg["ai_enabled"]:
        raise AIError("ai_disabled")

    existing = await store.analysis_for_alert(ctx.db, alert_id)
    if existing and not force:
        return _public(existing)

    alert = await store.alert_by_id(ctx.db, alert_id)
    if not alert:
        raise AIError("alert_not_found")

    shared = await get_ai_provider(ctx.settings)
    provider = str(shared.get("provider") or "").lower().replace("-", "_")
    if provider not in ALLOWED_PROVIDERS:
        raise AIError("sonnet_provider_required")
    api_key = shared.get("api_key")
    if not api_key:
        raise AIError("api_key_missing")

    model = str(radar_cfg.get("ai_model") or SONNET_MODEL)
    if not model.startswith("claude-sonnet-"):
        raise AIError("sonnet_model_required")
    base_url = str(shared.get("base_url") or DEFAULT_BASE_URLS[provider]).rstrip("/")

    context = await store.analysis_context(ctx.db, alert)
    payload = json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)
    input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    used = await store.reserve_ai_call(
        ctx.db, period=period, limit=int(radar_cfg["ai_monthly_limit"])
    )
    if used is None:
        raise AIError("monthly_limit_reached")

    body = {
        "model": model,
        "max_tokens": 1600,
        "system": SYSTEM_PROMPT,
        "thinking": {"type": "disabled"},
        "tools": [ANALYSIS_TOOL],
        "tool_choice": {"type": "tool", "name": ANALYSIS_TOOL["name"]},
        "messages": [
            {
                "role": "user",
                "content": "Проанализируй подтверждённую просадку по этим данным:\n" + payload,
            }
        ],
    }
    headers = {"anthropic-version": "2023-06-01"}
    if provider == "qcode":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["x-api-key"] = str(api_key)

    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(60.0)}
    if shared.get("proxy"):
        kwargs["proxy"] = shared["proxy"]
    async with httpx.AsyncClient(**kwargs) as http:
        response = await http.post(f"{base_url}/v1/messages", json=body, headers=headers)
    if response.status_code >= 400:
        raise AIError(f"http_{response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AIError("non_json_response") from exc
    if data.get("stop_reason") == "refusal":
        raise AIError("refused")
    tool_input = next(
        (
            block.get("input")
            for block in (data.get("content") or [])
            if block.get("type") == "tool_use"
            and block.get("name") == ANALYSIS_TOOL["name"]
            and isinstance(block.get("input"), dict)
        ),
        None,
    )
    if tool_input is None:
        raise AIError("tool_result_missing")
    result = _validate(tool_input)
    saved = await store.save_analysis(
        ctx.db,
        alert_id=alert_id,
        result=result,
        provider=provider,
        model=model,
        input_hash=input_hash,
    )
    return _public(saved)
