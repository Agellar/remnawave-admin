"""Отсев легального P2P из торрент-вердиктов.

nDPI опознаёт протокол, а не намерение. Игровые лаунчеры раздают обновления
по самому настоящему BitTorrent — Gaijin (War Thunder) первым делом, — и
вердикт по ним верный: это действительно torrent. Отличить такую раздачу от
пиратской качалки по трафику невозможно, различие только в том, КУДА идёт
обмен: у лаунчера это адреса самого издателя, у роя — случайные абоненты.

Поэтому фильтруем по организации-владельцу адреса. Сюда же попадают ложные
срабатывания эвристики на шифрованном потоке: пойманный случай — сервер
Kaspersky, которому приписали BitTorrent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from shared.db.connections import torrent_destination_ip

logger = logging.getLogger(__name__)

#: Ключ настройки со списком маркеров (подстроки имени организации, через запятую).
SETTING_KEY = "torrent_asn_whitelist"
_LOOKUP_TIMEOUT_SECONDS = 10.0

#: Кто раздаёт обновления по P2P или ловится эвристикой на шифрованном
#: потоке. Сравнение по вхождению и без регистра: имена организаций в базах
#: пишутся вразнобой («Kaspersky Lab Switzerland GmbH», «Valve Corp.»).
DEFAULT_MARKERS = (
    "kaspersky", "gaijin", "blizzard", "valve", "wargaming",
    "microsoft", "epic games", "riot games", "steam",
)


def markers() -> tuple[str, ...]:
    """Маркеры из настройки; пустая настройка — дефолтный список."""
    try:
        from shared.config_service import config_service

        raw = config_service.get(SETTING_KEY, "") or ""
    except Exception:
        logger.debug("torrent whitelist: настройка недоступна", exc_info=True)
        raw = ""
    if not isinstance(raw, str):
        raw = ""
    custom = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return custom or DEFAULT_MARKERS


def is_whitelisted_org(asn_org: str | None) -> bool:
    if not isinstance(asn_org, str) or not asn_org.strip():
        return False
    org = asn_org.lower()
    return any(marker in org for marker in markers())


async def filter_destinations(destinations: Iterable[str]) -> list[str]:
    """Оставить адреса, которые НЕ принадлежат легальным P2P-раздачам.

    Адрес приходит как ``ip:port`` — в том же виде, в каком его пишет Xray.
    Резолв идёт по сгруппированным адресам окна, а не по каждому событию.
    Неизвестный владелец и недоступная геобаза не доказывают нарушение:
    событие остаётся в сырой истории, но не наполняет пороги обвинения.
    """
    targets = [str(d) for d in destinations if d]
    if not targets:
        return []

    by_ip = {d: ip for d in targets if (ip := torrent_destination_ip(d)) is not None}
    if not by_ip:
        return []
    try:
        from shared.geoip import get_geoip_service

        found = await asyncio.wait_for(
            get_geoip_service().lookup_batch(list(set(by_ip.values()))),
            timeout=_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception as error:
        logger.warning("torrent whitelist: обвинение отложено (%s)", type(error).__name__)
        return []

    kept: list[str] = []
    for destination, ip in by_ip.items():
        info = found.get(ip)
        asn_org = getattr(info, "asn_org", None) if info else None
        if not isinstance(asn_org, str) or not asn_org.strip():
            continue
        if is_whitelisted_org(asn_org):
            logger.info("Torrent: разрешённый P2P исключён из доказательного окна")
            continue
        kept.append(destination)
    return kept
