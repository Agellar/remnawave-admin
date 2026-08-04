/**
 * /plugins/retention-radar/settings — пороги сегментов.
 *
 * Все поля — целые дни (кроме периода пересчёта). Значения по умолчанию
 * подобраны под месячные подписки; менять их приходится редко, поэтому
 * форма простая: список полей и одна кнопка сохранения.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeft, Loader2 } from '@/components/brand/icons'

import LicenseBanner from '@/components/plugins/license'
import { asLicenseError, fetchSettings, saveSettings } from './api'
import { Skeleton } from './primitives'
import type { ThresholdSettings } from './types'

const FIELDS: (keyof ThresholdSettings)[] = [
  'discount_expiring',
  'discount_silent',
  'discount_lapsed',
  'discount_stalled',
  'offer_valid_hours',
  'message_cooldown_days',
  'max_recipients_per_campaign',
  'skip_days_before_expire',
  'skip_days_after_expire',
  'silent_days',
  'silent_deep_days',
  'expiring_days',
  'expiring_risk_days',
  'lapsed_days',
  'onboarding_days',
  'trend_days',
  'snapshot_recompute_seconds',
]

export default function SettingsPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Partial<ThresholdSettings>>({})

  const { data, isLoading, error } = useQuery({
    queryKey: ['retention-radar-settings'],
    queryFn: fetchSettings,
    retry: false,
  })

  useEffect(() => {
    if (data) setDraft(data)
  }, [data])

  const mutation = useMutation({
    mutationFn: saveSettings,
    onSuccess: (saved) => {
      setDraft(saved)
      queryClient.invalidateQueries({ queryKey: ['retention-radar-overview'] })
      toast.success(t('plugins.retention_radar.settings.saved'))
    },
    onError: () => toast.error(t('plugins.retention_radar.settings.save_failed')),
  })

  const licenseError = useMemo(() => (error ? asLicenseError(error) : null), [error])
  if (licenseError) return <LicenseBanner error={licenseError} />

  if (isLoading || !data) return <Skeleton className="h-96 w-full" />

  return (
    <div className="space-y-6 max-w-2xl">
      <Link
        to="/plugins/retention-radar"
        className="inline-flex items-center gap-1.5 text-xs text-dark-300 hover:text-white"
      >
        <ArrowLeft className="w-3.5 h-3.5" aria-hidden />
        {t('plugins.retention_radar.back')}
      </Link>

      <div>
        <h1 className="text-xl font-semibold text-white">
          {t('plugins.retention_radar.settings.title')}
        </h1>
        <p className="mt-1 text-sm text-dark-300">
          {t('plugins.retention_radar.settings.subtitle')}
        </p>
      </div>

      <div className="glass-card p-5 space-y-4">
        {FIELDS.map((field) => (
          <label key={field} className="flex items-center justify-between gap-4">
            <span className="min-w-0">
              <span className="block text-sm text-dark-100">
                {t(`plugins.retention_radar.settings.fields.${field}`)}
              </span>
              <span className="block text-xs text-dark-400">
                {t(`plugins.retention_radar.settings.hints.${field}`)}
              </span>
            </span>
            {/* Ноль осмыслен только для скидок: кампания без скидки — это
                просто напоминание. Окна и пороги нулём ломаются. */}
            <input
              type="number"
              min={field.startsWith('discount_') ? 0 : 1}
              value={draft[field] ?? ''}
              onChange={(e) =>
                setDraft({ ...draft, [field]: Number(e.target.value) })
              }
              className="w-28 shrink-0 rounded border border-[var(--glass-border)] bg-[var(--glass-bg)] px-2.5 py-1.5 text-sm text-white tabular-nums"
            />
          </label>
        ))}
      </div>

      <button
        type="button"
        onClick={() => mutation.mutate(draft)}
        disabled={mutation.isPending}
        className="inline-flex items-center gap-2 rounded bg-primary-500/90 px-4 py-2 text-sm text-white hover:bg-primary-500 disabled:opacity-50"
      >
        {mutation.isPending && <Loader2 className="w-4 h-4 animate-spin" aria-hidden />}
        {t('plugins.retention_radar.settings.save')}
      </button>
    </div>
  )
}
