"""Генерация текста промо-кампании.

Пишет не «рекламу вообще», а сообщение под конкретный сегмент: человеку,
который молча перестал заходить, и человеку, у которого завтра сгорит
подписка, нужны разные слова.

Провайдер тот же, что у Smart Support: если у плагина нет своего ключа,
берём его настройки из окружения — ключ у оператора один, заводить второй
только ради второго плагина незачем.

Текст всегда возвращается оператору на правку. Автоотправки без просмотра
человеком здесь нет и быть не должно: это сообщение уходит живым людям.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URLS = {
    "qcode": "https://api.qcode.cc/api",
    "qcode_openai": "https://api.qcode.cc/openai/v1",
    "qcode_gemini": "https://api.qcode.cc/gemini",
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com",
}

DEFAULT_MODELS = {
    "qcode": "claude-sonnet-4-6",
    "anthropic": "claude-opus-5",
    "qcode_openai": "gpt-5.6-terra",
    "qcode_gemini": "gemini-2.5-flash",
    "gemini": "gemini-2.5-flash",
}

BEARER_ANTHROPIC = ("qcode",)
GEMINI_LIKE = ("gemini", "qcode_gemini")
OPENAI_LIKE = ("openai", "deepseek", "openrouter", "qcode_openai", "custom")

# Чем сегмент отличается от соседнего — это и есть вся суть письма.
SEGMENT_BRIEF = {
    "silent": (
        "Человек оплатил подписку, она ещё действует, но он перестал "
        "подключаться. Он ничего не просил и не жаловался. Цель письма — "
        "вернуть его в сервис, а не продать: подписка у него уже есть."
    ),
    "expiring": (
        "Подписка заканчивается на днях. Человек ещё пользуется сервисом. "
        "Цель — продлить до того, как доступ пропадёт."
    ),
    "lapsed": (
        "Подписка закончилась недавно, человек не продлил и ушёл. "
        "Цель — вернуть, признав, что он уже уходил."
    ),
    "stalled": (
        "Человек оплатил, но ни разу не подключился — скорее всего застрял "
        "на установке приложения. Цель — довести до первого подключения, "
        "а не продавать ещё раз."
    ),
}

SYSTEM_PROMPT = (
    "Ты пишешь короткие дружелюбные сообщения клиентам VPN-сервиса от лица "
    "поддержки. Сообщение уходит в Telegram-бот, через который человек "
    "покупал подписку.\n\n"
    "Тон: тёплый и живой, как пишет человек, а не рассылочный робот. "
    "Радуемся возвращению, а не продаём.\n\n"
    "Правила:\n"
    "1. 2-4 предложения. Без приветствия по имени и без подписи.\n"
    "2. На «вы». Эмодзи — 2-3 штуки на всё сообщение, по делу: одно в "
    "начале, одно рядом со скидкой. Сплошной ряд смайликов и восклицаний "
    "выглядит спамом.\n"
    "3. Никакого давления и выдуманного дефицита: «осталось 2 места», "
    "«только сегодня» — запрещено, если этого нет в брифе.\n"
    "4. Скидка называется ровно та, что дана в брифе. Не придумывай другие "
    "цифры, сроки и условия.\n"
    "5. Не упоминай, что человека выбрал алгоритм, и не пересказывай "
    "статистику его подключений — это выглядит слежкой.\n"
    "6. Под сообщением уже есть кнопка «{button_label}». Позови нажать её, "
    "но не пересказывай её текст и не давай других инструкций.\n"
    "7. НИЧЕГО НЕ ОБЕЩАЙ ОТ ИМЕНИ СИСТЕМЫ. Кнопка всего лишь открывает "
    "раздел подписки в боте, где человека уже ждёт скидка. Она НЕ "
    "подключает доступ, НЕ продлевает подписку сама и НЕ запускает "
    "оплату. Запрещены обороты вида «всё подключится заново», «доступ "
    "восстановится автоматически», «пришлём ссылку на оплату», «напишем "
    "вам», «менеджер свяжется» — ничего этого не произойдёт.\n"
    "8. Не описывай, что случится после нажатия. Просто позови нажать.\n"
    "9. Никакой разметки, кроме обычного текста и эмодзи: ни markdown, "
    "ни HTML.\n"
    "10. Пиши на языке: {language}."
)

# Обещания, которых система не выполнит. Промпт их запрещает, но проверка
# нужна отдельно: одна такая фраза в рассылке на 57 человек — это 57
# обманутых ожиданий и столько же обращений в поддержку.
RISKY_PATTERNS = [
    r"подключ\w*\s+(заново|автоматически|сраз)",
    r"(всё|все)\s+(снова\s+)?(зараб\w+|подключ\w+|восстанов\w+)",
    r"восстанов\w*\s+(автоматически|сам\w*)",
    r"(пришл|отправ|скин)\w*\s+(вам\s+)?(ссылк|счёт|счет|реквизит|инструкц)",
    r"(напиш|свяж)\w*\s+(вам|с\s+вами)",
    r"менеджер\w*\s+свяж\w+",
    r"спишем|спишет|автоматическ\w+\s+оплат",
]

# Просьба про JSON нужна только там, где форму ответа больше нечем задать.
# На протоколе Anthropic её добавлять НЕЛЬЗЯ: форму уже задаёт схема
# инструмента, и модель, послушав обе инструкции, кладёт JSON внутрь JSON —
# получателю уходит текст вместе с фигурными скобками.
JSON_TAIL = '\n\nОтветь строго одним JSON-объектом: {"message": "текст сообщения"}'

MAX_MESSAGE_CHARS = 700


class AIError(Exception):
    """Модель не ответила или ответила не тем."""


def get_config() -> Dict[str, Any]:
    """Конфиг провайдера: свой, иначе общий со Smart Support."""

    def env(name: str) -> Optional[str]:
        for prefix in ("RETENTION_RADAR_AI_", "SMART_SUPPORT_AI_"):
            value = os.environ.get(prefix + name)
            if value and value.strip():
                return value.strip()
        return None

    provider = (env("PROVIDER") or "qcode").strip().lower().replace("-", "_")
    return {
        "provider": provider,
        "api_key": env("API_KEY"),
        "model": env("MODEL") or DEFAULT_MODELS.get(provider) or "",
        "base_url": env("BASE_URL") or DEFAULT_BASE_URLS.get(provider) or "",
        "proxy": env("PROXY"),
        "language": env("REPLY_LANGUAGE") or "русский",
    }


def build_prompt(
    segment: str, discount_percent: int, valid_hours: int, recipients: int
) -> str:
    brief = SEGMENT_BRIEF.get(segment, "")
    offer = (
        f"скидка {discount_percent}% на продление, действует {valid_hours} часов"
        if discount_percent > 0
        else "без скидки, только напоминание"
    )
    return (
        f"Сегмент: {segment}\n"
        f"Ситуация: {brief}\n"
        f"Предложение: {offer}\n"
        f"Получателей: {recipients}\n\n"
        "Напиши текст сообщения."
    )


async def generate_message(
    cfg: Dict[str, Any],
    segment: str,
    discount_percent: int,
    valid_hours: int,
    recipients: int,
    button_label: str = "Вернуться со скидкой",
) -> str:
    provider = cfg["provider"]
    if not cfg.get("api_key"):
        raise AIError("api_key_missing")
    if not cfg.get("model") or not cfg.get("base_url"):
        raise AIError("provider_misconfigured")

    system = SYSTEM_PROMPT.format(
        language=cfg.get("language") or "русский",
        button_label=button_label,
    )
    prompt = build_prompt(segment, discount_percent, valid_hours, recipients)

    kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(60.0)}
    if cfg.get("proxy"):
        kwargs["proxy"] = cfg["proxy"]

    async def once(extra: str = "") -> str:
        async with httpx.AsyncClient(**kwargs) as http:
            body_prompt = prompt + extra
            if provider in GEMINI_LIKE:
                raw = await _gemini(http, cfg, system + JSON_TAIL, body_prompt)
            elif provider in OPENAI_LIKE:
                raw = await _openai_like(http, cfg, system + JSON_TAIL, body_prompt)
            else:
                raw = await _anthropic(http, cfg, system, body_prompt)

        parsed = _parse_json(raw)
        text = ""
        if isinstance(parsed, dict):
            text = str(parsed.get("message") or "").strip()
        if not text:
            # Модель могла ответить голым текстом — это тоже годится,
            # оператор всё равно прочитает его перед отправкой.
            text = raw.strip()
        return _unwrap(text)

    message = await once()
    risky = find_risky(message)
    if risky:
        # Одна попытка переписать с прямым указанием на проблему. Дальше
        # не зацикливаемся: текст всё равно уйдёт оператору на проверку,
        # а бесконечные ретраи только жгут квоту.
        message = await once(
            "\n\nВ прошлой версии было ложное обещание: "
            + "; ".join(risky)
            + ". Перепиши без обещаний того, что система сделает сама."
        )

    if not message:
        raise AIError("empty_response")
    return message[:MAX_MESSAGE_CHARS]


def find_risky(message: str) -> List[str]:
    """Куски текста, которые обещают несуществующие действия."""
    found: List[str] = []
    for pattern in RISKY_PATTERNS:
        for match in re.finditer(pattern, message, re.IGNORECASE):
            found.append(match.group(0))
    return found


def _unwrap(message: str) -> str:
    """Снять случайную JSON-обёртку вокруг текста.

    Страховка на случай, если провайдер всё же обернёт ответ ещё раз:
    в чат человеку такое уходить не должно ни при каких обстоятельствах.
    """
    for _ in range(2):
        if not message.startswith("{"):
            break
        inner = _parse_json(message)
        if not isinstance(inner, dict) or not inner.get("message"):
            break
        message = str(inner["message"]).strip()
    return message


# ── провайдеры ───────────────────────────────────────────────────

REPLY_TOOL = {
    "name": "submit_message",
    "description": "Вернуть текст сообщения клиенту.",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
}


def _ok(resp: httpx.Response) -> Dict[str, Any]:
    if resp.status_code >= 400:
        raise AIError(f"http_{resp.status_code}:{resp.text[:200]}")
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise AIError("non_json_response") from exc


async def _anthropic(http, cfg, system: str, prompt: str) -> str:
    """Форсированный tool call: релеи подмешивают свой системный промпт и
    глушат просьбу «верни JSON» — схема инструмента это переживает."""
    bearer = cfg["provider"] in BEARER_ANTHROPIC
    headers = {"anthropic-version": "2023-06-01"}
    headers["Authorization" if bearer else "x-api-key"] = (
        f"Bearer {cfg['api_key']}" if bearer else cfg["api_key"]
    )
    body = {
        "model": cfg["model"],
        "max_tokens": 1024,
        "system": system,
        "thinking": {"type": "disabled"},
        "tools": [REPLY_TOOL],
        "tool_choice": {"type": "tool", "name": REPLY_TOOL["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _ok(await http.post(f"{cfg['base_url']}/v1/messages", json=body, headers=headers))
    blocks = data.get("content") or []
    for block in blocks:
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return json.dumps(block["input"], ensure_ascii=False)
    for block in blocks:
        if block.get("type") == "text":
            return block["text"]
    raise AIError("empty_response")


async def _openai_like(http, cfg, system: str, prompt: str) -> str:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        # Без явного false релей QCode отвечает SSE-потоком.
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    data = _ok(
        await http.post(
            f"{cfg['base_url']}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
        )
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("empty_response") from exc


async def _gemini(http, cfg, system: str, prompt: str) -> str:
    url = f"{cfg['base_url']}/v1beta/models/{cfg['model']}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
    }
    data = _ok(await http.post(url, json=body, headers={"x-goog-api-key": cfg["api_key"]}))
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError("empty_response") from exc


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
