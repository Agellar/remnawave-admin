"""Движок гипотез — детерминированная часть диагноза.

Правила считаются до ИИ и без него: даже с выключенным ИИ отчёт отвечает
на вопрос «почему у клиента не работает». ``rule_id`` совпадают с ключами
``plugins.smart_support.rules.*`` в локалях панели — фронт подставит
локализованный заголовок, а ``detail`` мы всегда шлём свой, с цифрами.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .data import AGGRESSIVE_OEM

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _h(rule_id: str, title: str, detail: Optional[str], severity: str,
       confidence: float, suggested: Optional[str] = None) -> Dict[str, Any]:
    return {
        "rule_id": rule_id,
        "title": title,
        "detail": detail,
        "severity": severity,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "suggested_action": suggested,
    }


def evaluate(
    *,
    user: Dict[str, Any],
    history: Dict[str, Any],
    client: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    violations: List[Dict[str, Any]],
    thresholds: Dict[str, float],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    out.extend(_subscription(user))
    out.extend(_traffic(user, thresholds))
    out.extend(_nodes(nodes, thresholds))
    out.extend(_client(client))
    out.extend(_clusters(correlations))
    out.extend(_device(user, history))
    out.extend(_devices_limit(user))

    out.sort(key=lambda h: (SEVERITY_ORDER[h["severity"]], -h["confidence"]))
    return out


def _subscription(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    days = user.get("days_until_expire")
    status = (user.get("status") or "").upper()

    if days is not None and days < 0:
        return [_h(
            "subscription_expired",
            "Подписка истекла",
            f"Срок закончился {abs(days)} дн. назад — клиент отключён по плану.",
            "high", 1.0, "extend_subscription",
        )]
    if status in ("DISABLED", "LIMITED"):
        label = "заблокирован администратором" if status == "DISABLED" else "отключён по лимиту"
        return [_h(
            "user_disabled",
            "Юзер отключён в панели",
            f"Статус в панели — {status} ({label}). Пока статус не ACTIVE, подключения не будет.",
            "high", 0.95, "enable_user",
        )]
    if days is not None and 0 <= days <= 2:
        return [_h(
            "subscription_expired",
            "Подписка вот-вот кончится",
            f"Осталось {days} дн. — если клиент пишет сейчас, скоро отвалится совсем.",
            "medium", 0.5, "extend_subscription",
        )]
    return []


def _traffic(user: Dict[str, Any], t: Dict[str, float]) -> List[Dict[str, Any]]:
    percent = (user.get("traffic") or {}).get("percent")
    if percent is None:
        return []
    if percent >= t["traffic_full"]:
        return [_h(
            "traffic_exhausted", "Трафик исчерпан",
            f"Израсходовано {percent:.0f}% лимита — трафик кончился полностью.",
            "high", t["traffic_full_confidence"], "reset_traffic",
        )]
    if percent >= t["traffic_high"]:
        return [_h(
            "traffic_exhausted", "Трафик на исходе",
            f"Израсходовано {percent:.0f}% лимита тарифа.",
            "medium", t["traffic_high_confidence"], "reset_traffic",
        )]
    return []


def _nodes(nodes: List[Dict[str, Any]], t: Dict[str, float]) -> List[Dict[str, Any]]:
    """Смотрим в первую очередь на ноду, где юзер сидит прямо сейчас; если
    активной нет — на последнюю, через которую он ходил."""
    if not nodes:
        return []
    active = [n for n in nodes if n["user_active_here"]] or nodes[:1]
    out: List[Dict[str, Any]] = []

    for node in active:
        name = node["name"] or node["uuid"][:8]

        if node["is_disabled"]:
            out.append(_h(
                "node_disconnected", "Нода отключена",
                f"Нода «{name}» выключена в панели — клиента на ней быть не должно.",
                "high", 0.9, "switch_node",
            ))
            continue
        if not node["is_connected"]:
            out.append(_h(
                "node_disconnected", "Нода недоступна",
                f"Нода «{name}» не в статусе connected.",
                "high", 0.9, "switch_node",
            ))
            continue

        age = node.get("metrics_age_seconds")
        if age is not None and age > t["node_metrics_stale_seconds"]:
            out.append(_h(
                "node_disconnected", "Нода не присылает метрики",
                f"Последние метрики ноды «{name}» — {int(age // 60)} мин назад, похоже на зависание агента.",
                "medium", 0.6, "switch_node",
            ))

        cpu = node.get("cpu_usage")
        mem = node.get("memory_usage")
        if cpu is not None and cpu >= t["node_cpu_critical"]:
            out.append(_h(
                "node_overloaded", "Нода перегружена",
                f"CPU ноды «{name}» — {cpu:.0f}%, это критический уровень.",
                "high", 0.85, "switch_node",
            ))
        elif cpu is not None and cpu >= t["node_cpu_high"]:
            out.append(_h(
                "node_overloaded", "Нода под нагрузкой",
                f"CPU ноды «{name}» — {cpu:.0f}%.",
                "medium", 0.6, "switch_node",
            ))
        elif mem is not None and mem >= t["node_memory_high"]:
            out.append(_h(
                "node_overloaded", "Память ноды на пределе",
                f"Занято {mem:.0f}% RAM на ноде «{name}».",
                "medium", 0.55, "switch_node",
            ))
    return out


def _client(client: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if client.get("is_outdated"):
        out.append(_h(
            "client_version_outdated", "Клиент устарел",
            f"{client.get('last_app')} {client.get('last_version')} — не последняя версия.",
            "medium", 0.5, "notify_update",
        ))

    days = client.get("days_since_last_request")
    if days is not None and days >= 7:
        out.append(_h(
            "subscription_not_updated", "Клиент давно не обновлял подписку",
            f"Последний запрос конфига — {days} дн. назад. Если клиент жалуется сейчас, "
            f"скорее всего у него не обновился список серверов.",
            "medium", 0.5,
        ))
    return out


def _clusters(correlations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    active = [
        item for item in correlations
        if item.get("is_active", True) and item.get("kind") == "node"
    ]
    if not active:
        return []
    top = max(active, key=lambda c: c["affected_users"])
    where = f"нода «{top['label']}»"
    return [_h(
        "affected_by_cluster", "Массовая проблема",
        f"Проблема не только у него: {where}, затронуто пользователей — {top['affected_users']}. "
        f"Чинить надо инфраструктуру, а не аккаунт.",
        "high", 0.8, "switch_node",
    )]


def _device(user: Dict[str, Any], history: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Прошивки Xiaomi/Huawei/OnePlus и подобных выгружают VPN из фона.
    Признак — много коротких сессий с Android-устройства."""
    devices = user.get("_devices_raw") or []
    reconnects = history.get("total_connections", 0)
    if reconnects < 40:
        return []

    for d in devices:
        label = " ".join(str(x) for x in (d.get("platform"), d.get("device_model")) if x)
        low = label.lower()
        if "android" not in low and not any(oem in low for oem in AGGRESSIVE_OEM):
            continue
        oem = next((o for o in AGGRESSIVE_OEM if o in low), None)
        vendor = oem.capitalize() if oem else "Android-устройство"
        return [_h(
            "device_power_saving", "Прошивка выгружает VPN в фоне",
            f"{vendor}: {reconnects} переподключений за сутки. Такие прошивки закрывают "
            f"фоновые приложения ради экономии батареи — туннель рвётся сам по себе.",
            "medium", 0.6, "notify_battery_optimization",
        )]
    return []


def _devices_limit(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    devices = user.get("hwid_devices") or []
    limit = user.get("hwid_limit")

    if any(d["is_blacklisted"] for d in devices):
        out.append(_h(
            "hwid_blacklisted", "Устройство в чёрном списке",
            "Одно из устройств юзера заблокировано по HWID — с него подключиться не выйдет.",
            "high", 0.9,
        ))
    # Ровно N из N — штатное использование оплаченного лимита, не abuse.
    # Диагностируем лишь реальное превышение (например, при рассинхронизации).
    if limit and len(devices) > limit:
        out.append(_h(
            "hwid_limit_reached", "Превышен лимит устройств",
            f"Привязано {len(devices)} устройств при лимите {limit}. "
            f"Проверьте синхронизацию HWID и удалённые записи.",
            "medium", 0.7,
        ))
    return out
