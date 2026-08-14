/**
 * Types mirroring the block-radar plugin backend schemas
 * (``rwa_plugin_block_radar/schemas.py``).
 */

export interface RadarTick {
  at?: string
  ok?: boolean
  error?: string
  note?: string
  nodes_total?: number
  nodes_skipped?: number
  links_active?: number
  links_zero?: number
  cells?: number
  accepted?: number
  rejected?: number
  alerts_locked?: boolean
  alerts_new?: number
  alerts_resolved?: number
  notified?: number
  external_monitoring?: boolean
}

/**
 * Локальная просадка: онлайн ноды обвалился, а панель в целом жива.
 * Считается по доле ноды в общем онлайне, поэтому ночной спад — когда
 * проседают все ноды разом — сюда не попадает.
 */
export interface RadarNodeDip {
  node_uuid: string
  node_name: string | null
  since: string
  online: number
  baseline_online: number
  share: number
  baseline_share: number
  node_alive: boolean
}

export interface RadarStatus {
  last_tick: RadarTick | null
  last_probe?: RadarProbeCycleStatus | null
  globalping_configured?: boolean
  open_alerts: number
  license_usable: boolean
  open_dips: RadarNodeDip[]
  license_state: string | null
  license_tier: string | null
  /** Unix-время окончания подписки: нужно, чтобы предупредить до отключения. */
  license_paid_until: number | null
}

export interface RadarAffected {
  nodes?: string[]
  online_now?: number | null
  lost?: boolean
}

export interface RadarAlert {
  id: number
  kind: 'block' | 'operator_outage' | 'hoster_outage'
  /**
   * operator — известно, у чьих абонентов пропал доступ; hoster — просел весь
   * хостер, а разбор по операторам не набрал людей. Старый плагин поля не
   * шлёт, такие алерты считаем операторскими.
   */
  scope?: 'operator' | 'hoster'
  op_asn: number
  op_org?: string | null
  host_asn: number
  host_org?: string | null
  transport: string
  since: string
  resolved_at?: string | null
  panels?: number | null
  online?: number | null
  baseline?: number | null
  outage_summary?: string | null
  affected: RadarAffected
  ai_analysis?: RadarAIAnalysis | null
}

export interface RadarProbeCycleStatus {
  ok: boolean
  target_name?: string
  state?: RadarProbeState
  targets?: number
  ru?: string
  nodes?: string
  incident_open?: boolean
  error?: string | null
}

export type RadarProbeState =
  | 'healthy'
  | 'degraded'
  | 'regional_suspect'
  | 'endpoint_down'
  | 'insufficient'

export interface RadarProbeResult {
  source: 'globalping' | 'node'
  vantage_label: string
  country?: string | null
  asn?: number | null
  network?: string | null
  success: boolean
  latency_ms?: number | null
  error_code?: string | null
}

export interface RadarProbeTarget {
  target_uuid: string
  target_name: string
  target_port: number
  state: RadarProbeState
  ru_success: number
  ru_total: number
  control_success: number
  control_total: number
  node_success: number
  node_total: number
  consecutive_failures: number
  incident_open: boolean
  sampled_at: string
  error_code?: string | null
  results: RadarProbeResult[]
}

export interface RadarProbes {
  configured: boolean
  last_cycle?: RadarProbeCycleStatus | null
  items: RadarProbeTarget[]
}

export type RadarAIClassification =
  | 'likely_block'
  | 'provider_outage'
  | 'node_failure'
  | 'traffic_shift'
  | 'insufficient_data'

export interface RadarAIAnalysis {
  id: number
  alert_id: number
  classification: RadarAIClassification
  confidence: number
  summary: string
  evidence: string[]
  recommendations: string[]
  support_note?: string | null
  provider: string
  model: string
  created_at: string
  updated_at: string
}

export interface RadarAIStatus {
  enabled: boolean
  auto_analyze: boolean
  configured: boolean
  provider?: string | null
  model: string
  used: number
  monthly_limit: number
}

export interface QCodeUsageKey {
  name?: string | null
  is_active: boolean
  expires_at?: string | null
  expires_at_display?: string | null
  expires_in_days?: number | null
  expiry_warning: 'ok' | 'warning' | 'critical' | 'expired' | 'unknown'
  current_daily_cost?: number | null
  formatted_current_cost?: string | null
  current_requests: number
  current_tokens: number
  daily_cost_limit?: number | null
  has_monthly_quota: boolean
  monthly_cost_limit?: number | null
  monthly_cost_used?: number | null
  monthly_cost_percentage?: number | null
  opus_weekly_cost?: number | null
  opus_weekly_limit?: number | null
  is_near_cost_limit: boolean
  is_near_opus_limit: boolean
  has_error: boolean
  error_code?: string | null
}

export interface QCodeUsageStatus {
  configured: boolean
  ok: boolean
  error?: string | null
  account?: {
    active_api_keys: number
    total_api_keys: number
    today_cost_all_keys?: number | null
    formatted_today_cost?: string | null
    has_any_errors: boolean
    last_updated?: string | null
  } | null
  keys: QCodeUsageKey[]
  fetched_at?: string | null
  cache_ttl_seconds?: number | null
}

export interface RadarAlerts {
  items: RadarAlert[]
  total: number
}

export interface RadarSettings {
  notify_enabled: boolean
  notify_resolved: boolean
  online_window_minutes: number
  send_org_names: boolean
  /** Быстрое локальное правило по просадке доли ноды. */
  dip_enabled: boolean
  /** Слать ли уведомление, когда просевшая нода не на связи (плановый ребут). */
  dip_notify_offline: boolean
  dip_frac: number
  dip_min_users: number
  dip_confirm_ticks: number
  dip_history_days: number
  globalping_enabled: boolean
  node_probe_enabled: boolean
  probe_interval_seconds: number
  probe_confirm_cycles: number
  probe_min_ru_results: number
  node_probe_vantages: number
  probe_timeout_seconds: number
  ai_enabled: boolean
  ai_auto_analyze: boolean
  ai_model: string
  ai_monthly_limit: number
}

export type RadarSettingsPatch = Partial<RadarSettings>

/** Один хостер в рейтинге. Аварии и блокировки разведены намеренно:
 *  за первое отвечает хостер, за второе — нет, но выбирать площадку
 *  приходится с оглядкой на оба. */
export interface RadarHoster {
  asn: number
  org: string | null
  mine: boolean
  panels: number
  observed_hours: number
  operators: number
  outages: number
  blocks: number
  blocked_operators: number
  outages_per_month: number
  blocks_per_month: number
  median_recovery_minutes: number | null
}

export interface RadarHosters {
  window_days: number
  min_panels: number
  hosters: RadarHoster[]
  /** Хостеры, которых пока видит слишком мало панелей. Без этого числа
   *  пустая таблица читается как «данных нет», а не «сеть копится». */
  pending: number
  locked?: boolean
}

/** Сводка «что радар сейчас наблюдает» — агрегат с сервера сети. */
export interface RadarSite {
  host_asn: number
  host_org?: string | null
  transport: string
  /** Онлайн площадки в последнем часе; null — вердикта ещё нет. */
  online?: number | null
  /** Норма, с которой сравнивается онлайн; null — не набралось истории. */
  baseline?: number | null
  /** Сколько панелей сети наблюдают этот же ASN. */
  panels: number
}

export interface RadarOverview {
  links: {
    total: number
    /** Норма посчитана — маршрут под настоящим наблюдением. */
    measured: number
    /** Нормы хватает, чтобы детектор мог сработать. */
    armed: number
  }
  operators: number
  hosters: number
  sites: RadarSite[]
  network: { panels: number; hosters: number; operators: number }
  /** Пульс сети: чужой опыт по площадкам, которых у вас может и не быть. */
  pulse: {
    days: number
    incidents: number
    hosters: number
    blocks: number
    outages: number
    hosters_top: RadarProblemHoster[]
  }
}

export interface RadarProblemHoster {
  host_asn: number
  host_org?: string | null
  incidents: number
  blocks: number
  outages: number
  last_at?: string | null
  /** Площадка самого владельца — повод присмотреться, а не «не переезжать». */
  is_mine: boolean
}
