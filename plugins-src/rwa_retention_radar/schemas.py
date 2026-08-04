"""Формы ответов API. Совпадают с
``web/frontend/src/plugins/retention-radar/types.ts``."""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SegmentUser(BaseModel):
    uuid: str
    username: Optional[str] = None
    telegram_id: Optional[int] = None
    status: Optional[str] = None
    expire_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    last_online: Optional[datetime] = None
    days_silent: Optional[int] = None
    days_until_expire: Optional[int] = None
    used_traffic_bytes: Optional[int] = None
    traffic_limit_bytes: Optional[int] = None
    tag: Optional[str] = None
    # Внутри сегмента: этот случай хуже остальных (тишина перед
    # истечением, глубокое молчание). Фронт красит строку.
    at_risk: bool = False


class TrendPoint(BaseModel):
    day: date
    users_count: int


class SegmentCard(BaseModel):
    key: str
    users_count: int
    # Дельта ко вчерашнему срезу. None, если истории ещё нет —
    # рисовать «+0» в первый день работы плагина нечестно.
    delta: Optional[int] = None
    trend: List[TrendPoint] = Field(default_factory=list)


class OverviewResponse(BaseModel):
    generated_at: datetime
    total_users: int
    active_users: int
    segments: List[SegmentCard] = Field(default_factory=list)
    attention: List[SegmentUser] = Field(default_factory=list)
    # С какого дня копится история срезов — фронт по ней понимает,
    # можно ли верить графику.
    history_since: Optional[date] = None


class SegmentResponse(BaseModel):
    key: str
    total: int
    limit: int
    offset: int
    users: List[SegmentUser] = Field(default_factory=list)


class ThresholdSettings(BaseModel):
    silent_days: float
    silent_deep_days: float
    expiring_days: float
    expiring_risk_days: float
    lapsed_days: float
    onboarding_days: float
    snapshot_recompute_seconds: float
    trend_days: float
    discount_expiring: float
    discount_silent: float
    discount_lapsed: float
    discount_stalled: float
    offer_valid_hours: float
    message_cooldown_days: float
    max_recipients_per_campaign: float
    skip_days_before_expire: float
    skip_days_after_expire: float


# ── кампании ─────────────────────────────────────────────────────

class CampaignPreviewIn(BaseModel):
    segment: str
    # Если оператор уже правил текст — генерировать заново не нужно,
    # иначе его правки молча потеряются.
    message_text: Optional[str] = None


class SkipReasons(BaseModel):
    no_telegram: int = 0
    cooldown: int = 0
    over_limit: int = 0
    # Сейчас в работе у автоматики Bedolaga — трогать нельзя.
    bedolaga_auto: int = 0


class CampaignPreviewOut(BaseModel):
    segment: str
    recipients: int
    skipped: SkipReasons
    discount_percent: int
    offer_valid_hours: int
    message_text: str
    # Текст кнопки под сообщением — оператор должен видеть, что именно
    # получит человек, а не только текст.
    button_label: str = ""
    # Пусто, если не настроен RETENTION_RADAR_MINIAPP_URL — тогда
    # сообщение уйдёт вовсе без кнопки.
    button_url: Optional[str] = None
    ai_error: Optional[str] = None
    # Найденные в тексте обещания, которых система не выполнит.
    warnings: List[str] = Field(default_factory=list)
    sample: List[SegmentUser] = Field(default_factory=list)
    # Отпечаток предпросмотра: без него отправка не пройдёт.
    confirm_token: str


class CampaignSendIn(BaseModel):
    segment: str
    message_text: str = Field(..., min_length=1, max_length=4000)
    confirm_token: str
    # По умолчанию именно пробный прогон: случайный POST не должен
    # заканчиваться рассылкой двум сотням человек.
    dry_run: bool = True


class CampaignSendOut(BaseModel):
    campaign_id: int
    dry_run: bool
    recipients: int
    offers_created: int = 0
    offer_errors: int = 0
    broadcast_id: Optional[int] = None
    status: str


class CampaignRecord(BaseModel):
    id: int
    segment: str
    discount_percent: int
    valid_hours: int
    message_text: str
    recipients: int
    sent: int
    failed: int
    dry_run: bool
    status: str
    broadcast_id: Optional[int] = None
    admin_username: Optional[str] = None
    created_at: datetime
    finished_at: Optional[datetime] = None


class CampaignHistoryResponse(BaseModel):
    items: List[CampaignRecord] = Field(default_factory=list)


class ThresholdPatch(BaseModel):
    values: Dict[str, float]
