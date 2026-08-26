import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Clock, ShieldAlert } from '@/components/brand/icons'

import {
  fetchFreshness,
  fetchIncidents,
  fetchOperations,
  fetchQuality,
  reviewIncident,
  updateWorkflow,
} from './api'
import type { IncidentReviewLabel, IncidentWorkflowStatus } from './types'
import type { OperationalContext } from './types'

const WORKFLOW: IncidentWorkflowStatus[] = ['acknowledged', 'investigating', 'snoozed']
const REVIEWS: IncidentReviewLabel[] = ['confirmed', 'false_positive', 'unclear']

export default function IncidentCenterPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [active, setActive] = useState(true)
  const incidents = useQuery({
    queryKey: ['incident-center', active],
    queryFn: () => fetchIncidents(active),
    retry: false,
    refetchInterval: 60_000,
  })
  const freshness = useQuery({
    queryKey: ['incident-center-freshness'],
    queryFn: fetchFreshness,
    retry: false,
    refetchInterval: 60_000,
  })
  const quality = useQuery({
    queryKey: ['incident-center-quality'],
    queryFn: fetchQuality,
    retry: false,
  })
  const operations = useQuery({
    queryKey: ['incident-center-operations'],
    queryFn: fetchOperations,
    retry: false,
    refetchInterval: 60_000,
  })
  const workflow = useMutation({
    mutationFn: ({ id, status }: { id: number; status: IncidentWorkflowStatus }) =>
      updateWorkflow(id, status, status === 'snoozed' ? 60 : undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['incident-center'] }),
  })
  const review = useMutation({
    mutationFn: ({ id, label }: { id: number; label: IncidentReviewLabel }) =>
      reviewIncident(id, label),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['incident-center'] })
      qc.invalidateQueries({ queryKey: ['incident-center-quality'] })
    },
  })

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-amber-400" />
            <h1 className="text-xl font-semibold text-white">{t('plugins.incident_center.title')}</h1>
          </div>
          <p className="mt-1 text-sm text-dark-300">{t('plugins.incident_center.subtitle')}</p>
        </div>
        <div className="flex rounded border border-[var(--glass-border)] p-1 text-xs">
          {[true, false].map((value) => (
            <button key={String(value)} onClick={() => setActive(value)} className={`rounded px-3 py-1.5 ${active === value ? 'bg-amber-500/20 text-amber-200' : 'text-dark-300'}`}>
              {t(value ? 'plugins.incident_center.active' : 'plugins.incident_center.history')}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <FreshnessCard title={t('plugins.incident_center.history_source')} source={freshness.data?.history} />
        <FreshnessCard title={t('plugins.incident_center.radar_source')} source={freshness.data?.radar} />
      </div>

      {operations.data && <OperationsCard data={operations.data} />}

      {quality.data && quality.data.length > 0 && (
        <div className="glass-card p-4">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-dark-200">{t('plugins.incident_center.quality')}</h2>
          <div className="mt-3 grid gap-2 md:grid-cols-3">
            {quality.data.map((item) => (
              <div key={`${item.source_plugin}-${item.kind}`} className="rounded border border-[var(--glass-border)] p-3 text-xs">
                <div className="text-white">{item.source_plugin} · {item.kind}</div>
                <div className="mt-1 text-dark-300">{t('plugins.incident_center.quality_counts', {
                  reviewed: item.reviewed,
                  confirmed: item.confirmed,
                  false_positive: item.false_positive,
                  unclear: item.unclear,
                })}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-3">
        {incidents.isLoading && <div className="glass-card p-5 text-sm text-dark-300">{t('common.loading')}</div>}
        {incidents.data?.items.length === 0 && <div className="glass-card p-5 text-sm text-dark-300">{t('plugins.incident_center.empty')}</div>}
        {incidents.data?.items.map((incident) => (
          <article key={incident.id} className="glass-card p-4 space-y-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <AlertTriangle className={`h-4 w-4 ${incident.severity === 'critical' || incident.severity === 'high' ? 'text-red-400' : 'text-amber-400'}`} />
                  <h2 className="text-sm font-medium text-white">{incident.title}</h2>
                </div>
                <div className="mt-1 text-[11px] text-dark-400">
                  {incident.source_plugin} · {incident.kind} · {incident.transport ?? '—'} · {new Date(incident.updated_at).toLocaleString()}
                </div>
              </div>
              <span className="rounded bg-white/5 px-2 py-1 text-[10px] uppercase text-dark-200">{incident.operational_status}</span>
            </div>

            <div className="flex flex-wrap gap-2 text-[11px]">
              {WORKFLOW.map((status) => (
                <button key={status} disabled={workflow.isPending || incident.status === 'resolved'} onClick={() => workflow.mutate({ id: incident.id, status })} className="rounded border border-[var(--glass-border)] px-2 py-1 text-dark-200 hover:text-white disabled:opacity-40">
                  {t(`plugins.incident_center.workflow.${status}`)}
                </button>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2 border-t border-white/5 pt-3 text-[11px]">
              <span className="text-dark-400">{t('plugins.incident_center.review')}:</span>
              {REVIEWS.map((label) => (
                <button key={label} onClick={() => review.mutate({ id: incident.id, label })} className={`rounded px-2 py-1 ${incident.review_label === label ? 'bg-emerald-500/20 text-emerald-200' : 'border border-[var(--glass-border)] text-dark-200'}`}>
                  {t(`plugins.incident_center.reviews.${label}`)}
                </button>
              ))}
            </div>
          </article>
        ))}
      </div>
    </div>
  )
}

function OperationsCard({ data }: { data: OperationalContext }) {
  const { t } = useTranslation()
  const rollout = data.agent_rollout
  const audit = data.audit
  return (
    <div className="glass-card p-4 space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-dark-200">
        {t('plugins.incident_center.operations')}
      </h2>
      <div className="grid gap-3 text-xs md:grid-cols-3">
        <div>
          <div className="text-dark-400">{t('plugins.incident_center.agent_rollout')}</div>
          <div className={rollout.complete ? 'mt-1 text-emerald-300' : 'mt-1 text-amber-300'}>
            {t('plugins.incident_center.agent_compatible', {
              compatible: rollout.compatible,
              total: rollout.total,
              version: rollout.minimum_version,
            })}
          </div>
        </div>
        <div>
          <div className="text-dark-400">{t('plugins.incident_center.restart_context')}</div>
          <div className="mt-1 text-white">
            {t('plugins.incident_center.restart_count', {
              count: audit.operator_restarts.length,
              hours: audit.window_hours,
            })}
          </div>
        </div>
        <div>
          <div className="text-dark-400">{t('plugins.incident_center.throttle_audit')}</div>
          <div className="mt-1 text-white">
            +{audit.throttle_added} / −{audit.throttle_removed}
          </div>
        </div>
      </div>
      {audit.restore_failures.length > 0 && (
        <p className="rounded border border-red-500/30 bg-red-500/[0.08] p-2 text-xs text-red-200">
          {t('plugins.incident_center.restore_failures', { count: audit.restore_failures.length })}
        </p>
      )}
      <p className="text-[11px] text-dark-400">
        {t('plugins.incident_center.operations_evidence')}
      </p>
    </div>
  )
}

function FreshnessCard({ title, source }: { title: string; source?: { fresh: boolean; state: string; age_seconds?: number | null; max_age_minutes: number } }) {
  const { t } = useTranslation()
  const age = source?.age_seconds == null ? '—' : `${Math.round(source.age_seconds / 60)}m`
  return (
    <div className="glass-card p-4 flex items-center justify-between gap-3">
      <div><div className="text-xs text-dark-300">{title}</div><div className="mt-1 text-sm text-white">{age} / {source?.max_age_minutes ?? '—'}m</div></div>
      <div className={`flex items-center gap-1 text-xs ${source?.fresh ? 'text-emerald-300' : 'text-amber-300'}`}>
        {source?.fresh ? <Check className="h-4 w-4" /> : <Clock className="h-4 w-4" />}
        {t(source?.fresh ? 'plugins.incident_center.fresh' : 'plugins.incident_center.stale')}
      </div>
    </div>
  )
}
