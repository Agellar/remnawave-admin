export type IncidentWorkflowStatus = 'new' | 'acknowledged' | 'investigating' | 'snoozed'
export type IncidentReviewLabel = 'confirmed' | 'false_positive' | 'unclear'

export interface IncidentItem {
  id: number
  source_plugin: string
  kind: string
  severity: string
  title: string
  details: Record<string, unknown>
  node_uuid?: string | null
  transport?: string | null
  status: string
  workflow_status: IncidentWorkflowStatus
  operational_status: string
  snoozed_until?: string | null
  assigned_to?: string | null
  review_label?: IncidentReviewLabel | null
  review_note?: string | null
  updated_by?: string | null
  started_at: string
  updated_at: string
  resolved_at?: string | null
}

export interface IncidentList {
  items: IncidentItem[]
  total: number
}

export interface FreshnessSource {
  fresh: boolean
  state: 'fresh' | 'stale' | 'missing' | 'empty'
  newest_at?: string | null
  age_seconds?: number | null
  max_age_minutes: number
}

export interface SystemFreshness {
  ok: boolean
  history: FreshnessSource
  radar: FreshnessSource
}

export interface OperationalContext {
  agent_rollout: {
    source_state: 'available' | 'missing'
    minimum_version: string
    total: number
    compatible: number
    versions: Record<string, number>
    outdated: Array<{ node_name: string; agent_version: string }>
    unknown: string[]
    complete: boolean
  }
  audit: {
    source_state: 'available' | 'missing'
    window_hours: number
    throttle_added: number
    throttle_removed: number
    operator_restarts: Array<{
      audit_event_id: number
      node_name?: string | null
      observed_at?: string | null
    }>
    restore_failures: Array<{
      audit_event_id: number
      observed_at?: string | null
      evidence: Record<string, boolean>
    }>
  }
}

export interface QualityItem {
  source_plugin: string
  kind: string
  reviewed: number
  confirmed: number
  false_positive: number
  unclear: number
}
