"""Pydantic-модели ответов плагина.

Зеркалят ``web/frontend/src/plugins/smart-support/types.ts`` панели —
порядок и имена полей менять нельзя, фронт собран под них.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MatchedBy = Literal["uuid", "short_uuid", "telegram_id", "email", "ip", "username", "fallback"]
Severity = Literal["low", "medium", "high"]
AIProviderName = Literal["qcode", "gemini", "groq", "openrouter", "anthropic"]


# ── поиск ────────────────────────────────────────────────────────

class SearchHit(BaseModel):
    uuid: str
    short_uuid: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    telegram_id: Optional[int] = None
    status: Optional[str] = None
    expire_at: Optional[datetime] = None
    last_connection_at: Optional[datetime] = None
    last_country: Optional[str] = None
    last_asn: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    matched_by: MatchedBy
    total: int
    hits: List[SearchHit] = Field(default_factory=list)


# ── отчёт ────────────────────────────────────────────────────────

class TrafficUsage(BaseModel):
    limit_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    percent: Optional[float] = None


class HwidDevice(BaseModel):
    hwid: str
    platform: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    is_blacklisted: bool = False


class UserSection(BaseModel):
    uuid: str
    short_uuid: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    telegram_id: Optional[int] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    expire_at: Optional[datetime] = None
    days_until_expire: Optional[int] = None
    subscription_uuid: Optional[str] = None
    subscription_url: Optional[str] = None
    traffic: TrafficUsage = Field(default_factory=TrafficUsage)
    active_squads: List[str] = Field(default_factory=list)
    hwid_limit: Optional[int] = None
    hwid_devices: List[HwidDevice] = Field(default_factory=list)


class ConnectionEvent(BaseModel):
    connected_at: datetime
    disconnected_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    ip: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    asn: Optional[str] = None
    asn_org: Optional[str] = None
    node_uuid: Optional[str] = None
    node_name: Optional[str] = None


class HistorySection(BaseModel):
    total_connections: int = 0
    unique_ips: int = 0
    unique_countries: int = 0
    unique_asns: int = 0
    anomalies: List[str] = Field(default_factory=list)
    timeline: List[ConnectionEvent] = Field(default_factory=list)


class ClientSection(BaseModel):
    last_app: Optional[str] = None
    last_version: Optional[str] = None
    raw_user_agent: Optional[str] = None
    last_request_at: Optional[datetime] = None
    is_outdated: Optional[bool] = None
    days_since_last_request: Optional[int] = None
    # Синк истории запросов подписки встал: показываем дату, но не делаем
    # из неё выводов — см. SOURCE_STALE_AFTER_MINUTES в data.py.
    source_stale: bool = False
    source_newest_at: Optional[datetime] = None


class NodeCard(BaseModel):
    uuid: str
    name: Optional[str] = None
    address: Optional[str] = None
    is_connected: bool = False
    is_disabled: bool = False
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    disk_usage: Optional[float] = None
    metrics_age_seconds: Optional[int] = None
    user_active_here: bool = False


class CorrelationCluster(BaseModel):
    kind: Literal["asn", "node", "asn_node"]
    key: str
    label: Optional[str] = None
    affected_users: int
    window_start: datetime
    window_end: datetime
    total_users: Optional[int] = None
    is_active: bool = True
    age_minutes: Optional[int] = None


class ViolationCard(BaseModel):
    id: int
    created_at: datetime
    score: Optional[float] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    # ``action`` is the compatibility alias used by older frontends.
    action: Optional[str] = None
    recommended_action: Optional[str] = None
    action_taken: Optional[str] = None
    is_resolved: bool = False


class ViolationRecap(BaseModel):
    window_days: int = Field(default=30, ge=1, le=365)
    total: int = Field(default=0, ge=0)
    unresolved: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)
    annulled: int = Field(default=0, ge=0)
    last_at: Optional[datetime] = None


class ThrottleCard(BaseModel):
    active: bool = True
    rate_kbit: int
    reason: Optional[str] = None
    created_at: Optional[datetime] = None
    until: Optional[datetime] = None


class IncidentCard(BaseModel):
    id: int
    source_plugin: str
    kind: str
    severity: str
    title: str
    details: Dict[str, Any] = Field(default_factory=dict)
    node_uuid: Optional[str] = None
    transport: Optional[str] = None
    status: str
    started_at: datetime
    updated_at: datetime


class Hypothesis(BaseModel):
    rule_id: str
    title: str
    detail: Optional[str] = None
    severity: Severity = "low"
    confidence: float = 0.0
    suggested_action: Optional[str] = None


class AIAnalysis(BaseModel):
    summary: str
    extra_hypotheses: List[Hypothesis] = Field(default_factory=list)
    confidence: Severity = "medium"
    # Готовое сообщение клиенту — саппорт копирует его в чат как есть.
    reply_draft: Optional[str] = None
    provider_used: str
    model: Optional[str] = None


class ReportResponse(BaseModel):
    generated_at: datetime
    user: UserSection
    history_24h: HistorySection
    client: ClientSection
    nodes: List[NodeCard] = Field(default_factory=list)
    throttle: Optional[ThrottleCard] = None
    correlations: List[CorrelationCluster] = Field(default_factory=list)
    violations_recent: List[ViolationCard] = Field(default_factory=list)
    violations_recap: Optional[ViolationRecap] = None
    incidents_active: List[IncidentCard] = Field(default_factory=list)
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    ai_analysis: Optional[AIAnalysis] = None
    session_id: Optional[int] = None


class FeedbackIn(BaseModel):
    session_id: Optional[int] = None
    user_uuid: str
    rule_id: str = Field(..., min_length=1, max_length=128)
    verdict: Literal["correct", "partial", "wrong", "resolved"]
    comment: Optional[str] = Field(default=None, max_length=500)


class FeedbackOut(BaseModel):
    id: int
    rule_id: str
    verdict: str
    summary: Dict[str, int] = Field(default_factory=dict)


# ── настройки ────────────────────────────────────────────────────

class ThresholdSettings(BaseModel):
    """Все поля опциональны: PUT принимает частичный патч, GET отдаёт
    полностью разрешённый набор (дефолты + переопределения из БД)."""

    node_cpu_high: Optional[float] = None
    node_cpu_critical: Optional[float] = None
    node_memory_high: Optional[float] = None
    node_metrics_stale_seconds: Optional[float] = None
    traffic_high: Optional[float] = None
    traffic_full: Optional[float] = None
    traffic_full_confidence: Optional[float] = None
    traffic_high_confidence: Optional[float] = None
    cluster_node_window_minutes: Optional[float] = None
    cluster_node_reconnects_per_user: Optional[float] = None
    cluster_node_min_affected: Optional[float] = None
    cluster_asn_window_minutes: Optional[float] = None
    cluster_asn_min_affected: Optional[float] = None
    correlation_recompute_seconds: Optional[float] = None
    correlation_max_age_minutes: Optional[float] = None
    cluster_node_min_share: Optional[float] = None
    cluster_node_min_total_users: Optional[float] = None
    correlation_history_minutes: Optional[float] = None


class AIProviderSettings(BaseModel):
    provider: AIProviderName
    key_set: bool = False
    model: Optional[str] = None


class AISettingsOut(BaseModel):
    enabled: bool
    provider_chain: List[AIProviderName] = Field(default_factory=list)
    providers: List[AIProviderSettings] = Field(default_factory=list)
    outage_lookup_enabled: bool = True


class AISettingsIn(BaseModel):
    enabled: Optional[bool] = None
    provider_chain: Optional[List[AIProviderName]] = None
    keys: Optional[Dict[str, str]] = None
    models: Optional[Dict[str, str]] = None
    outage_lookup_enabled: Optional[bool] = None


class AIQuota(BaseModel):
    period_limit: int
    used: int
    topup_left: int = 0


class AIProviderStatus(BaseModel):
    # Legacy DB/env configs may still use custom/qcode_openai/qcode_gemini.
    # The modern settings editor remains constrained to AIProviderName.
    provider: str
    configured: bool = False
    available: bool = False
    cooldown_seconds_remaining: int = 0
    last_error: Optional[str] = None


class AIStatusResponse(BaseModel):
    enabled: bool
    configured: bool = False
    providers: List[AIProviderStatus] = Field(default_factory=list)
    # Backward-compatible extras for the older custom diagnostics endpoint.
    subscription_state: Optional[Literal["active", "grace", "expired", "missing"]] = None
    quota: Optional[AIQuota] = None


class AIProviderOut(BaseModel):
    """Наш дополнительный эндпоинт — стоковый UI его не рисует, но он
    нужен, чтобы менять провайдера без правки .env и рестарта."""

    provider: str
    model: Optional[str] = None
    base_url: Optional[str] = None
    proxy: Optional[str] = None
    has_api_key: bool = False
    monthly_limit: int = 0
    source: Literal["db", "env", "none"] = "none"


class AIProviderIn(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    proxy: Optional[str] = None
    api_key: Optional[str] = None
    monthly_limit: Optional[int] = None


# ── общий справочник клиентских приложений ──────────────────────

class ClientIssue(BaseModel):
    id: Optional[int] = None
    version_min: Optional[str] = None
    version_max: Optional[str] = None
    platform: Optional[str] = None
    os_min: Optional[str] = None
    os_max: Optional[str] = None
    severity: Severity = "medium"
    title: str = Field(..., min_length=1, max_length=300)
    detail: Optional[str] = Field(default=None, max_length=4000)
    workaround: Optional[str] = Field(default=None, max_length=4000)


class ClientApp(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=200)
    latest_version: Optional[str] = Field(default=None, max_length=64)
    ambiguous: bool = False
    notes: Optional[str] = Field(default=None, max_length=4000)
    issues: List[ClientIssue] = Field(default_factory=list)


class ClientReferenceResponse(BaseModel):
    apps: List[ClientApp] = Field(default_factory=list)
    catalog_version: int = 0


class ClientSubmission(BaseModel):
    id: int
    kind: Literal["app", "issue"]
    app_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    comment: str = ""
    status: Literal["pending", "accepted", "rejected"] = "pending"
    votes_up: int = 0
    votes_down: int = 0
    my_vote: int = Field(default=0, ge=-1, le=1)
    mine: bool = False
    created_at: str


class ClientSubmissionListResponse(BaseModel):
    submissions: List[ClientSubmission] = Field(default_factory=list)


class ClientSubmissionIn(BaseModel):
    kind: Literal["app", "issue"]
    app_id: str = Field(..., min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)
    comment: Optional[str] = Field(default=None, max_length=500)


class ClientSubmissionCreated(BaseModel):
    id: int


class ClientVoteIn(BaseModel):
    vote: int = Field(..., ge=-1, le=1)


class ClientVoteOut(BaseModel):
    votes_up: int = 0
    votes_down: int = 0
    my_vote: int = Field(default=0, ge=-1, le=1)


# ── действия ─────────────────────────────────────────────────────

class ActionParamSpec(BaseModel):
    name: str
    type: Literal["number", "boolean", "string"]
    default: Optional[Any] = None
    label_i18n: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None


class ActionMetadata(BaseModel):
    id: str
    title_i18n: str
    severity: Literal["safe", "destructive"]
    requires_confirmation: bool = False
    params: List[ActionParamSpec] = Field(default_factory=list)


class ActionListResponse(BaseModel):
    actions: List[ActionMetadata] = Field(default_factory=list)


class ActionExecuteIn(BaseModel):
    user_uuid: str
    params: Dict[str, Any] = Field(default_factory=dict)
    triggered_by_rule_id: Optional[str] = None


class ActionExecuteOut(BaseModel):
    ok: bool
    message: str
    data: Optional[Dict[str, Any]] = None


# ── журнал ───────────────────────────────────────────────────────

class SessionEntry(BaseModel):
    id: int
    opened_at: datetime
    admin_username: Optional[str] = None
    target_user_uuid: Optional[str] = None
    triggered_by_rule_id: Optional[str] = None
    action_id: Optional[str] = None
    ok: Optional[bool] = None
    message: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class SessionListResponse(BaseModel):
    items: List[SessionEntry] = Field(default_factory=list)
    total: int = 0
