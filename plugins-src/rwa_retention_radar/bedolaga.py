"""Клиент Bedolaga Web API: скидки и рассылки.

Ходим через SSH-туннель (`bedolaga-tunnel:8080` внутри docker-сети) — сам
API слушает только 127.0.0.1 на своём сервере. Телеграм с сервера панели
недоступен, да и не нужен: писать людям может только тот бот, через
которого они покупали, а это Bedolaga.

Два вызова на кампанию:
1. ``/promo-offers/broadcast`` на каждого получателя — создаёт реальную
   персональную скидку с сроком действия;
2. ``/broadcasts`` со списком telegram_id — доставляет текст. Батчинг,
   паузы и учёт заблокировавших бот делает сам Bedolaga.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = "http://bedolaga-tunnel:8080"

# Метка аудитории в истории рассылок Bedolaga. Префикс `external_`
# разрешён его схемой именно для случая «получатели пришли списком».
TARGET_PREFIX = "external_retention_"


class BedolagaError(Exception):
    """Проблема с вызовом Bedolaga API."""


def _client(cfg: Dict[str, Any]) -> httpx.AsyncClient:
    token = cfg.get("token")
    if not token:
        raise BedolagaError("token_missing")
    return httpx.AsyncClient(
        base_url=(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(60.0),
    )


def _ok(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        raise BedolagaError(f"http_{resp.status_code}:{resp.text[:200]}")
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise BedolagaError("non_json_response") from exc


async def health(cfg: Dict[str, Any]) -> Dict[str, Any]:
    async with _client(cfg) as http:
        return _ok(await http.get("/health"))


async def create_offer(
    cfg: Dict[str, Any],
    *,
    telegram_id: int,
    discount_percent: int,
    valid_hours: int,
    notification_type: str,
) -> Dict[str, Any]:
    """Персональная скидка одному человеку.

    ``notification_type`` — ключ оффера в Bedolaga: повторный вызов с тем
    же ключом обновляет неиспользованный оффер, а не плодит новые.
    """
    body = {
        "telegram_id": telegram_id,
        "discount_percent": discount_percent,
        "bonus_amount_kopeks": 0,
        "valid_hours": valid_hours,
        "notification_type": notification_type,
        "effect_type": "percent_discount",
    }
    async with _client(cfg) as http:
        return _ok(await http.post("/promo-offers/broadcast", json=body))


async def send_broadcast(
    cfg: Dict[str, Any],
    *,
    segment: str,
    message_text: str,
    telegram_ids: List[int],
    button_label: Optional[str] = None,
    button_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Текст кампании ровно нашему списку получателей.

    Кнопка открывает кабинет как Telegram Mini App по ссылке с
    ``?claim_offer=auto``: кабинет сам находит активное предложение
    пользователя, применяет скидку и ведёт на страницу тарифов. Человеку
    не нужно искать баннер и жать «Активировать».

    Конкретный id оффера в ссылку не зашиваем: рассылка уходит одним
    сообщением на всех, а предложение у каждого своё — кто открыл,
    кабинет узнаёт по initData Telegram.
    """
    if not telegram_ids:
        raise BedolagaError("empty_audience")
    body: Dict[str, Any] = {
        "target": f"{TARGET_PREFIX}{segment}",
        "message_text": message_text,
        # Свою кнопку кладём отдельно, штатные не добавляем: две кнопки
        # с похожим смыслом разбавляют призыв.
        "selected_buttons": [],
        "recipient_telegram_ids": telegram_ids,
    }
    if button_label and button_url:
        body["custom_buttons"] = [
            {
                "label": button_label,
                "action_type": "webapp",
                "action_value": button_url,
            }
        ]
    async with _client(cfg) as http:
        return _ok(await http.post("/broadcasts", json=body))


async def broadcast_status(cfg: Dict[str, Any], broadcast_id: int) -> Optional[Dict[str, Any]]:
    async with _client(cfg) as http:
        data = _ok(await http.get("/broadcasts", params={"limit": 20}))
    for item in data.get("items", []):
        if item.get("id") == broadcast_id:
            return item
    return None
