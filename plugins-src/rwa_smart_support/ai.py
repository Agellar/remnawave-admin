"""ИИ-разбор отчёта: свой ключ, свой провайдер, без чужого облака.

Провайдер выбирается настройкой: Gemini, любой OpenAI-совместимый эндпоинт
(DeepSeek, OpenRouter, локальная модель), Anthropic и релей QCode (``qcode``,
``qcode_openai``, ``qcode_gemini`` — один ключ ``cr_...`` на все протоколы).
Запрос идёт напрямую с сервера панели, при необходимости — через прокси:
с российских IP Anthropic и OpenAI закрыты гео-фильтром, и релей — как раз
способ обойтись без своего прокси.

Используем ``httpx`` — он уже есть в зависимостях панели. Официальные SDK
провайдеров тянуть нельзя: плагины ставятся через ``pip install --no-deps``,
так что доступны только библиотеки самой панели.

В облако уходит только компактный обезличенный контекст: ни IP, ни email,
ни Telegram ID, ни ссылка на подписку.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openai/gpt-4o-mini",
    "anthropic": "claude-opus-5",
    # QCode — релей: один ключ (`cr_...`) работает во всех трёх протоколах,
    # протокол выбирается путём запроса. Дефолты проверены живым вызовом
    # 04.08.2026 — релей отдаёт claude-sonnet-4-6/opus-5/sonnet-5/opus-4-8/
    # haiku-4-5, серию gpt-5.6 и Gemini. Модель задаётся SMART_SUPPORT_AI_MODEL.
    "qcode": "claude-sonnet-4-6",
    "qcode_openai": "gpt-5.6-terra",
    "qcode_gemini": "gemini-2.5-flash",
    "custom": "",
}

DEFAULT_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com",
    "qcode": "https://api.qcode.cc/api",
    "qcode_openai": "https://api.qcode.cc/openai/v1",
    "qcode_gemini": "https://api.qcode.cc/gemini",
}

# OpenAI-совместимые: один и тот же /chat/completions
OPENAI_LIKE = ("openai", "deepseek", "groq", "openrouter", "qcode_openai", "custom")

# Протокол Anthropic, но ключ уходит Bearer-ом вместо x-api-key —
# так его принимают релеи вроде QCode.
ANTHROPIC_LIKE_BEARER = ("qcode",)

# Язык черновика ответа клиенту. Меняется настройкой ai_reply_language
# (или SMART_SUPPORT_AI_REPLY_LANGUAGE) — у кого-то саппорт англоязычный.
DEFAULT_REPLY_LANGUAGE = "русский"

# Потолок длины черновика: это реплика в чат, а не статья.
MAX_REPLY_CHARS = 900

# Протокол Gemini (x-goog-api-key)
GEMINI_LIKE = ("gemini", "qcode_gemini")

# «Черновик ответа клиенту» пишется по другим правилам, чем разбор: разбор
# читает инженер, ответ — человек с той стороны тикета. Поэтому у него свой
# блок правил, а не приписка «ещё напиши сообщение юзеру».
REPLY_RULES = (
    "Отдельно составь reply_draft — готовое сообщение ЭТОМУ клиенту, "
    "которое саппорт скопирует в чат как есть.\n"
    "Правила для reply_draft:\n"
    "- Пиши на языке {language}, на «вы», спокойно и по-человечески.\n"
    "- 2-4 предложения. Без приветствия и подписи — их саппорт добавит сам.\n"
    "- Не раскрывай внутреннюю кухню: имена и адреса нод, ASN, номера версий "
    "панели, сколько ещё людей задето, чужие данные.\n"
    "- Не обещай сроков починки и не извиняйся трижды.\n"
    "- Если проблема на нашей стороне — скажи прямо, что видим её и чиним.\n"
    "- Если дело в клиенте или сети юзера — дай ОДИН конкретный шаг, "
    "который ему сделать (обновить подписку в приложении, переключиться "
    "на другую сеть, обновить приложение).\n"
    "- Если по сводке проблемы не видно — так и напиши: у нас всё в норме, "
    "и попроси уточнить, что именно не работает."
)

SYSTEM_PROMPT_TEMPLATE = (
    "Ты — инженер поддержки VPN-сервиса на базе панели Remnawave. "
    "На вход тебе дают обезличенную сводку по одному пользователю и гипотезы, "
    "которые уже посчитал детерминированный движок правил. "
    "Твоя задача — найти то, что движок пропустил: кросс-сигнальные выводы, "
    "которые видны только при сопоставлении разных секций отчёта.\n\n"
    "Правила:\n"
    "1. Не повторяй гипотезы движка — их саппорт уже видит.\n"
    "2. Не выдумывай данные, которых нет в сводке.\n"
    "3. Если добавить нечего — верни пустой extra_hypotheses и честное summary.\n"
    "4. summary и detail пиши по-русски, коротко, языком инженера поддержки.\n"
    "5. Это диагностика доступности, клиента и инфраструктуры, а НЕ поиск "
    "нарушителей. Несколько устройств, две и более страны, роуминг, CGNAT, "
    "смена ASN и необычное время подключения сами по себе нормальны и не "
    "доказывают передачу подписки или злоупотребление.\n"
    "6. violations_recent — выборка истории, а не список доказанных текущих "
    "нарушений. Записи с is_resolved=true — закрытая история; они не доказывают "
    "текущую проблему. Аннулированные срабатывания исключены из этой выборки. "
    "Никогда не предлагай блокировку, отключение, отзыв "
    "подписки или иное наказание.\n"
    "7. provider_outage, если он есть, — подтверждённый внешний сигнал сбоя "
    "сети провайдера клиента; учитывай его первым, но не расширяй выводы за "
    "пределы переданных данных.\n"
    "8. suggested_action может быть только switch_node, notify_update или null; "
    "эти подсказки не выполняются автоматически.\n"
    "9. administrative_throttle — уже применённая администратором мера, а не "
    "сбой ноды или провайдера. Она может объяснять низкую скорость. Не предлагай "
    "самовольно снять или обойти ограничение; только сообщи саппорту, что мера "
    "активна, если это относится к жалобе.\n"
    "10. В violations_recent строго различай recommended_action (рекомендация) "
    "и action_taken (реально выполненное действие). Не утверждай, что мера "
    "применена, если заполнена только рекомендация. Историческое action_taken "
    "тоже не доказывает, что ограничение действует сейчас: текущее состояние "
    "смотри в user и administrative_throttle.\n"
    "11. violations_recap — полный счёт за window_days: total не включает "
    "annulled, но включает resolved. annulled — ошибки детектора, а не рецидив. "
    "unresolved — ещё не разобранные сигналы, а не подтверждённое злоупотребление. "
    "violations_sample — ограниченная выборка за другой период, её размер "
    "нельзя выдавать за полный счёт. Ни повторяемость, ни высокий score сами "
    "по себе не объясняют недоступность сервиса.\n\n"
    "{reply_rules}\n\n"
    "Ответь СТРОГО одним JSON-объектом без markdown-обёртки:\n"
    '{{"summary": "2-4 предложения: что происходит с юзером", '
    '"confidence": "low|medium|high", '
    '"reply_draft": "сообщение клиенту", '
    '"extra_hypotheses": [{{"rule_id": "snake_case_идентификатор", '
    '"title": "короткий заголовок", "detail": "объяснение с цифрами из сводки", '
    '"severity": "low|medium|high", "confidence": 0.0-1.0, '
    '"suggested_action": null}}]}}'
)


def build_system_prompt(language: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        reply_rules=REPLY_RULES.format(language=language)
    )


# Схема ответа как инструмент. На протоколе Anthropic мы не просим модель
# «вернуть JSON текстом», а форсируем вызов инструмента: релеи (QCode)
# подмешивают в запрос свой системный промпт и глушат output_config —
# проверено, модель начинает отвечать прозой. Форсированный tool_choice
# такие вставки переживает, потому что форма ответа задана схемой.
ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Вернуть разбор проблемы пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 предложения: что происходит с юзером",
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reply_draft": {
                "type": "string",
                "description": "Готовое сообщение клиенту, 2-4 предложения, "
                               "без приветствия и подписи, без внутренних деталей.",
            },
            "extra_hypotheses": {
                "type": "array",
                "description": "Только то, чего нет в гипотезах движка. Может быть пустым.",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_id": {"type": "string"},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "confidence": {"type": "number"},
                        "suggested_action": {"type": "string"},
                    },
                    "required": ["rule_id", "title", "severity", "confidence"],
                },
            },
        },
        "required": ["summary", "confidence", "reply_draft", "extra_hypotheses"],
    },
}


class AIError(Exception):
    """Проблема с ИИ-вызовом. Отчёт при этом всё равно отдаётся — без секции."""


def build_context(report: Dict[str, Any]) -> Dict[str, Any]:
    """Компактный обезличенный контекст для модели.

    Из отчёта берём только то, что нужно для рассуждения: статусы, счётчики,
    страны, ноды. Персональные идентификаторы не уходят — ни IP, ни email,
    ни Telegram ID, ни ссылка на подписку, ни HWID.
    """
    user = report["user"]
    history = report["history_24h"]
    client = report["client"]

    devices = []
    for d in user.get("hwid_devices", [])[:5]:
        devices.append({"platform": d.get("platform"), "blacklisted": d.get("is_blacklisted")})

    timeline = history.get("timeline", [])
    durations = [e["duration_seconds"] for e in timeline if e.get("duration_seconds") is not None]
    outage = report.get("provider_outage")
    safe_outage = None
    if isinstance(outage, dict):
        safe_outage = {
            "asn": outage.get("asn"),
            "org": outage.get("org"),
            "severity": outage.get("severity"),
            "methods": list(outage.get("methods") or [])[:5],
            "source": outage.get("source"),
        }

    throttle = report.get("throttle")
    safe_throttle = None
    if isinstance(throttle, dict) and throttle.get("active"):
        safe_throttle = {
            "active": True,
            "rate_kbit": throttle.get("rate_kbit"),
            "reason": throttle.get("reason"),
            "until": throttle.get("until"),
        }

    # Defend the AI boundary as well as the SQL query: old/cached reports may
    # contain annulled records, and a historical action is not a current one.
    recent = [
        item for item in report.get("violations_recent", [])
        if isinstance(item, dict)
        and str(item.get("action_taken") or "").strip().lower() != "annulled"
    ]
    recap = report.get("violations_recap")
    safe_recap = None
    if isinstance(recap, dict):
        safe_recap = {
            key: recap.get(key)
            for key in ("window_days", "total", "unresolved", "resolved", "annulled")
        }

    return {
        "user": {
            "status": user.get("status"),
            "days_until_expire": user.get("days_until_expire"),
            "traffic_percent": (user.get("traffic") or {}).get("percent"),
            "squads": user.get("active_squads"),
            "devices_used": len(user.get("hwid_devices", [])),
            "devices_limit": user.get("hwid_limit"),
            "devices": devices,
        },
        "connections_24h": {
            "total": history.get("total_connections"),
            "unique_ips": history.get("unique_ips"),
            "unique_countries": history.get("unique_countries"),
            "unique_asns": history.get("unique_asns"),
            "countries": sorted({e["country"] for e in timeline if e.get("country")}),
            "asn_orgs": sorted({e["asn_org"] for e in timeline if e.get("asn_org")})[:10],
            "median_session_seconds": sorted(durations)[len(durations) // 2] if durations else None,
            "sessions_under_60s": sum(1 for d in durations if d < 60),
            "anomalies": history.get("anomalies"),
        },
        "client_app": {
            "app": client.get("last_app"),
            "version": client.get("last_version"),
            "outdated": client.get("is_outdated"),
            "days_since_last_config_fetch": client.get("days_since_last_request"),
        },
        "nodes": [
            {
                "name": n.get("name"),
                "connected": n.get("is_connected"),
                "disabled": n.get("is_disabled"),
                "cpu": n.get("cpu_usage"),
                "memory": n.get("memory_usage"),
                "metrics_age_seconds": n.get("metrics_age_seconds"),
                "user_active_here": n.get("user_active_here"),
            }
            for n in report.get("nodes", [])[:8]
        ],
        "mass_incidents": [
            {"kind": c["kind"], "label": c.get("label"), "affected_users": c["affected_users"]}
            for c in report.get("correlations", [])
            if c.get("kind") == "node" and c.get("is_active", True)
        ],
        "provider_outage": safe_outage,
        "administrative_throttle": safe_throttle,
        "violations_recap": safe_recap,
        "violations_sample": {
            "window_days": 14,
            "limit": 10,
            "returned": min(len(recent), 10),
            "is_complete_count": False,
        },
        "violations_recent": [
            {
                "score": item.get("score"),
                "recommended_action": item.get("recommended_action", item.get("action")),
                "action_taken": item.get("action_taken"),
                "is_resolved": bool(item.get("action_taken")),
            }
            for item in recent[:10]
        ],
        "engine_hypotheses": [
            {"rule_id": h["rule_id"], "title": h["title"], "confidence": h["confidence"]}
            for h in report.get("hypotheses", [])
        ],
    }


async def analyze(cfg: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    """Позвать модель и разобрать ответ. Бросает :class:`AIError`."""
    # Дефис и подчёркивание в имени провайдера равнозначны: «qcode-openai»
    # из .env не должен разъезжаться с «qcode_openai» из кода.
    provider = (cfg.get("provider") or "gemini").strip().lower().replace("-", "_")
    api_key = cfg.get("api_key")
    if not api_key:
        raise AIError("api_key_missing")

    model = cfg.get("model") or DEFAULT_MODELS.get(provider) or ""
    if not model:
        raise AIError("model_missing")
    base_url = (cfg.get("base_url") or DEFAULT_BASE_URLS.get(provider) or "").rstrip("/")
    if not base_url:
        raise AIError("base_url_missing")

    payload = json.dumps(build_context(report), ensure_ascii=False, default=str)
    user_prompt = f"Сводка по пользователю:\n{payload}"
    system_prompt = build_system_prompt(cfg.get("reply_language") or DEFAULT_REPLY_LANGUAGE)

    # The report itself must stay interactive even when a provider hangs.
    # API-level failover adds a total deadline; the transport timeout keeps a
    # cancelled/slow socket from occupying the pool for a full minute.
    kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(12.0, connect=5.0)}
    if cfg.get("proxy"):
        kwargs["proxy"] = cfg["proxy"]

    async with httpx.AsyncClient(**kwargs) as http:
        if provider in GEMINI_LIKE:
            raw = await _call_gemini(http, base_url, model, api_key, user_prompt, system_prompt)
        elif provider == "anthropic" or provider in ANTHROPIC_LIKE_BEARER:
            raw = await _call_anthropic(
                http, base_url, model, api_key, user_prompt, system_prompt,
                bearer=provider in ANTHROPIC_LIKE_BEARER,
            )
        elif provider in OPENAI_LIKE:
            raw = await _call_openai_like(
                http, base_url, model, api_key, user_prompt, system_prompt
            )
        else:
            raise AIError(f"unknown_provider:{provider}")

    parsed = _parse_json(raw)
    if parsed is None:
        raise AIError("bad_json")

    # Черновик обрезаем: в чат уходит короткое сообщение, а не полотно.
    # Пустая строка допустима — модель могла решить, что писать нечего.
    reply = str(parsed.get("reply_draft") or "").strip()[:MAX_REPLY_CHARS]

    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "extra_hypotheses": _clean_hypotheses(parsed.get("extra_hypotheses")),
        "confidence": _one_of(parsed.get("confidence"), ("low", "medium", "high"), "medium"),
        "reply_draft": reply or None,
        "provider_used": provider,
        "model": model,
    }


# ── провайдеры ───────────────────────────────────────────────────

async def _call_gemini(
    http, base_url: str, model: str, key: str, prompt: str, system: str
) -> str:
    url = f"{base_url}/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    resp = await http.post(url, json=body, headers={"x-goog-api-key": key})
    data = _ok(resp)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise AIError("empty_response") from e


async def _call_openai_like(
    http, base_url: str, model: str, key: str, prompt: str, system: str
) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # Явное false — не косметика: релей QCode без него отвечает
        # SSE-потоком (`data: [DONE]`) даже на обычный запрос.
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    resp = await http.post(
        f"{base_url}/chat/completions", json=body,
        headers={"Authorization": f"Bearer {key}"},
    )
    data = _ok(resp)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise AIError("empty_response") from e


async def _call_anthropic(
    http, base_url: str, model: str, key: str, prompt: str, system: str,
    *, bearer: bool = False
) -> str:
    """Messages API. Мышление выключаем: задача короткая, а на моделях
    Opus 5 оно включено по умолчанию и ест общий лимит max_tokens.

    ``bearer`` — для релеев (QCode), которые принимают ключ в
    ``Authorization``, а не в ``x-api-key``.
    """
    body = {
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "thinking": {"type": "disabled"},
        "tools": [ANALYSIS_TOOL],
        "tool_choice": {"type": "tool", "name": ANALYSIS_TOOL["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"anthropic-version": "2023-06-01"}
    headers["Authorization" if bearer else "x-api-key"] = (
        f"Bearer {key}" if bearer else key
    )
    resp = await http.post(f"{base_url}/v1/messages", json=body, headers=headers)
    data = _ok(resp)
    if data.get("stop_reason") == "refusal":
        raise AIError("refused")

    blocks = data.get("content") or []
    for block in blocks:
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return json.dumps(block["input"], ensure_ascii=False)
    # Запасной путь: если прокси проглотил tools, читаем текст как раньше.
    for block in blocks:
        if block.get("type") == "text":
            return block["text"]
    raise AIError("empty_response")


def _ok(resp: httpx.Response) -> Dict[str, Any]:
    if resp.status_code >= 400:
        raise AIError(f"http_{resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise AIError("non_json_response") from e


# ── разбор ответа ────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """Модели любят обернуть JSON в markdown, даже когда просишь не делать
    этого. Снимаем забор, иначе берём первый сбалансированный объект."""
    if not raw:
        return None
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _one_of(value: Any, allowed: tuple, default: str) -> str:
    value = str(value or "").lower()
    return value if value in allowed else default


def _clean_hypotheses(items: Any) -> List[Dict[str, Any]]:
    """Модель может вернуть что угодно — приводим к схеме фронта и режем
    длину, чтобы карточка отчёта не превращалась в простыню."""
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items[:5]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        rule_id = re.sub(r"[^a-z0-9_]+", "_", str(item.get("rule_id") or "ai_finding").lower())
        suggested = str(item.get("suggested_action") or "").strip().lower()
        if suggested not in {"switch_node", "notify_update"}:
            suggested = ""
        out.append({
            "rule_id": f"ai_{rule_id}" if not rule_id.startswith("ai_") else rule_id,
            "title": str(item["title"])[:120],
            "detail": str(item.get("detail") or "")[:600] or None,
            "severity": _one_of(item.get("severity"), ("low", "medium", "high"), "low"),
            "confidence": max(0.0, min(1.0, confidence)),
            "suggested_action": suggested or None,
        })
    return out
