/** Формы ответов плагина retention_radar (см. plugin/rwa_retention_radar/schemas.py). */

/** Ключи сегментов — они же в URL и в i18n. */
export type SegmentKey = 'silent' | 'expiring' | 'lapsed' | 'stalled'

export const SEGMENT_KEYS: SegmentKey[] = ['silent', 'expiring', 'lapsed', 'stalled']

export interface SegmentUser {
  uuid: string
  username?: string | null
  telegram_id?: number | null
  status?: string | null
  expire_at?: string | null
  created_at?: string | null
  last_online?: string | null
  days_silent?: number | null
  days_until_expire?: number | null
  used_traffic_bytes?: number | null
  traffic_limit_bytes?: number | null
  tag?: string | null
  /** Внутри сегмента этот случай хуже прочих — строка подсвечивается. */
  at_risk: boolean
}

export interface TrendPoint {
  day: string
  users_count: number
}

export interface SegmentCard {
  key: SegmentKey
  users_count: number
  /** null, пока нет вчерашнего среза: рисовать «+0» в первый день нечестно. */
  delta?: number | null
  trend: TrendPoint[]
}

export interface OverviewResponse {
  generated_at: string
  total_users: number
  active_users: number
  segments: SegmentCard[]
  attention: SegmentUser[]
  history_since?: string | null
  data_freshness: {
    fresh: boolean
    state: string
    newest_at?: string | null
    age_seconds?: number | null
    max_age_minutes: number
  }
}

export interface SegmentResponse {
  key: SegmentKey
  total: number
  limit: number
  offset: number
  users: SegmentUser[]
}

export interface ThresholdSettings {
  silent_days: number
  silent_deep_days: number
  expiring_days: number
  expiring_risk_days: number
  lapsed_days: number
  onboarding_days: number
  snapshot_recompute_seconds: number
  trend_days: number
  discount_expiring: number
  discount_silent: number
  discount_lapsed: number
  discount_stalled: number
  offer_valid_hours: number
  message_cooldown_days: number
  max_recipients_per_campaign: number
  skip_days_before_expire: number
  skip_days_after_expire: number
}

/** Причины, по которым человек из сегмента не попал в рассылку. */
export interface SkipReasons {
  no_telegram: number
  bedolaga_auto: number
  cooldown: number
  over_limit: number
  active_incident: number
  throttled: number
}

export interface CampaignArm {
  arm_token: string
  idempotency_key: string
  expires_in_seconds: number
}

export interface CampaignSafetySettings {
  live_campaigns_enabled: boolean
  require_server_arm: boolean
  arm_ttl_minutes: number
  suppress_active_incidents: boolean
  incident_lookback_minutes: number
  require_fresh_data: boolean
  max_data_age_minutes: number
}

export interface CampaignPreview {
  segment: SegmentKey
  recipients: number
  skipped: SkipReasons
  discount_percent: number
  offer_valid_hours: number
  message_text: string
  /** Текст кнопки под сообщением. */
  button_label: string
  ai_error?: string | null
  /** Найденные в тексте обещания, которых система не выполнит. */
  warnings: string[]
  sample: SegmentUser[]
  /** Отпечаток предпросмотра — без него отправка не пройдёт. */
  confirm_token: string
}

export interface CampaignResult {
  campaign_id: number
  dry_run: boolean
  recipients: number
  offers_created: number
  offer_errors: number
  broadcast_id?: number | null
  status: string
  skipped_throttled: number
}

export interface CampaignRecord {
  id: number
  segment: SegmentKey
  discount_percent: number
  valid_hours: number
  message_text: string
  recipients: number
  sent: number
  failed: number
  dry_run: boolean
  status: string
  broadcast_id?: number | null
  admin_username?: string | null
  created_at: string
  finished_at?: string | null
}
