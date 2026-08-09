import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Save, Sliders, Sparkles } from '@/components/brand/icons'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import LicenseBanner from '@/components/plugins/license'

import { asLicenseError, fetchAIStatus, fetchQCodeUsage, fetchSettings, updateSettings } from './api'
import type { RadarSettings, RadarSettingsPatch } from './types'

const TOGGLES: Array<keyof RadarSettings> = [
  'notify_enabled',
  'notify_resolved',
  'send_org_names',
  'dip_enabled',
  'dip_notify_offline',
  'ai_enabled',
  'ai_auto_analyze',
]

/**
 * /plugins/block-radar/settings — тумблеры радара.
 *
 * GET отдаёт эффективные значения (дефолты + переопределения), PUT
 * принимает только изменённые ключи.
 */
export default function SettingsPage() {
  const { t } = useTranslation()
  const qc = useQueryClient()

  const { data, isLoading, error } = useQuery({
    queryKey: ['block-radar-settings'],
    queryFn: fetchSettings,
    retry: false,
    staleTime: 10_000,
  })
  const aiStatus = useQuery({
    queryKey: ['block-radar-ai-status'],
    queryFn: fetchAIStatus,
    retry: false,
  })
  const qcodeUsage = useQuery({
    queryKey: ['block-radar-qcode-usage'],
    queryFn: fetchQCodeUsage,
    retry: false,
    staleTime: 60_000,
    refetchInterval: 300_000,
  })

  const licenseError = useMemo(() => (error ? asLicenseError(error) : null), [error])

  const [draft, setDraft] = useState<RadarSettings | null>(null)
  const [touched, setTouched] = useState<Set<keyof RadarSettings>>(new Set())

  useEffect(() => {
    if (data && touched.size === 0) setDraft(data)
  }, [data, touched.size])

  const mutation = useMutation({
    mutationFn: (patch: RadarSettingsPatch) => updateSettings(patch),
    onSuccess: (fresh) => {
      setDraft(fresh)
      setTouched(new Set())
      qc.setQueryData(['block-radar-settings'], fresh)
      toast.success(t('plugins.block_radar.settings.saved'))
    },
    onError: () => {
      toast.error(t('plugins.block_radar.settings.save_error'))
    },
  })

  const setValue = <K extends keyof RadarSettings>(key: K, value: RadarSettings[K]) => {
    setDraft((d) => (d ? { ...d, [key]: value } : d))
    setTouched((s) => new Set(s).add(key))
  }

  const onSave = () => {
    if (!draft || touched.size === 0) return
    const patch: RadarSettingsPatch = {}
    for (const key of touched) {
      ;(patch as Record<string, unknown>)[key] = draft[key]
    }
    mutation.mutate(patch)
  }

  if (licenseError) {
    return (
      <div className="space-y-6">
        <BackLink />
        <LicenseBanner error={licenseError} />
      </div>
    )
  }

  if (isLoading || !draft) {
    return (
      <div className="space-y-6">
        <BackLink />
        <div className="glass-card p-5">
          <div className="text-sm text-dark-300">{t('common.loading')}</div>
        </div>
      </div>
    )
  }

  const dirty = touched.size > 0

  return (
    <div className="space-y-6">
      <BackLink />

      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Sliders className="w-5 h-5 text-emerald-400 shrink-0" aria-hidden />
          <h1 className="text-xl sm:text-2xl font-bold text-white truncate">
            {t('plugins.block_radar.settings.title')}
          </h1>
        </div>
        <Button onClick={onSave} disabled={!dirty || mutation.isPending} className="shrink-0">
          <Save className="w-4 h-4 mr-2" aria-hidden />
          {t('plugins.block_radar.settings.save')}
        </Button>
      </div>

      <div className="glass-card p-5 space-y-5">
        {TOGGLES.map((key) => (
          <div key={key} className="flex items-start gap-3">
            <Switch
              id={key}
              checked={Boolean(draft[key])}
              disabled={mutation.isPending}
              onCheckedChange={(v) => setValue(key, v as RadarSettings[typeof key])}
            />
            <div>
              <Label htmlFor={key} className="text-sm text-white">
                {t(`plugins.block_radar.settings.fields.${key}.label`)}
              </Label>
              <p className="text-[11px] text-dark-400 mt-0.5">
                {t(`plugins.block_radar.settings.fields.${key}.help`)}
              </p>
            </div>
          </div>
        ))}

        <div className="space-y-1.5 max-w-xs">
          <Label htmlFor="online_window_minutes" className="text-xs text-dark-300">
            {t('plugins.block_radar.settings.fields.online_window_minutes.label')}
          </Label>
          <Input
            id="online_window_minutes"
            type="number"
            min={1}
            max={60}
            value={String(draft.online_window_minutes)}
            onChange={(e) => {
              const v = Number(e.target.value)
              if (!Number.isNaN(v)) setValue('online_window_minutes', v)
            }}
            className="h-9"
          />
          <p className="text-[11px] text-dark-400">
            {t('plugins.block_radar.settings.fields.online_window_minutes.help')}
          </p>
        </div>
      </div>

      <div className="glass-card p-5 space-y-4">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-violet-400" aria-hidden />
          <h2 className="text-sm font-semibold text-white uppercase tracking-wider">
            {t('plugins.block_radar.settings.ai_title')}
          </h2>
        </div>
        <div className="grid gap-3 sm:grid-cols-3 text-xs">
          <div>
            <div className="text-dark-400">{t('plugins.block_radar.settings.ai_model')}</div>
            <div className="text-white font-mono mt-1">{draft.ai_model}</div>
          </div>
          <div>
            <div className="text-dark-400">{t('plugins.block_radar.settings.ai_provider')}</div>
            <div className="text-white font-mono mt-1">
              {aiStatus.data?.configured ? aiStatus.data.provider : t('plugins.block_radar.settings.ai_not_configured')}
            </div>
          </div>
          <div>
            <div className="text-dark-400">{t('plugins.block_radar.settings.ai_usage')}</div>
            <div className="text-white font-mono mt-1">
              {aiStatus.data?.used ?? 0} / {draft.ai_monthly_limit}
            </div>
          </div>
        </div>
        <div className="space-y-1.5 max-w-xs">
          <Label htmlFor="ai_monthly_limit" className="text-xs text-dark-300">
            {t('plugins.block_radar.settings.fields.ai_monthly_limit.label')}
          </Label>
          <Input
            id="ai_monthly_limit"
            type="number"
            min={1}
            max={1000}
            value={String(draft.ai_monthly_limit)}
            onChange={(e) => {
              const v = Number(e.target.value)
              if (!Number.isNaN(v)) setValue('ai_monthly_limit', v)
            }}
            className="h-9"
          />
          <p className="text-[11px] text-dark-400">
            {t('plugins.block_radar.settings.fields.ai_monthly_limit.help')}
          </p>
        </div>
        <p className="text-[11px] text-dark-400">
          {t('plugins.block_radar.settings.ai_privacy')}
        </p>

        <div className="border-t border-white/10 pt-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-xs font-semibold text-white uppercase tracking-wider">
              {t('plugins.block_radar.settings.qcode_usage_title')}
            </h3>
            {qcodeUsage.data?.account?.last_updated && (
              <span className="text-[10px] text-dark-400">
                {t('plugins.block_radar.settings.qcode_updated', { value: qcodeUsage.data.account.last_updated })}
              </span>
            )}
          </div>

          {qcodeUsage.isLoading ? (
            <p className="text-xs text-dark-400">{t('common.loading')}</p>
          ) : !qcodeUsage.data?.configured ? (
            <p className="text-xs text-dark-400">{t('plugins.block_radar.settings.qcode_not_configured')}</p>
          ) : !qcodeUsage.data.ok ? (
            <p className="text-xs text-amber-300">
              {t('plugins.block_radar.settings.qcode_unavailable', { reason: qcodeUsage.data.error })}
            </p>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-3 text-xs">
                <div>
                  <div className="text-dark-400">{t('plugins.block_radar.settings.qcode_active_keys')}</div>
                  <div className="text-white font-mono mt-1">
                    {qcodeUsage.data.account?.active_api_keys ?? 0} / {qcodeUsage.data.account?.total_api_keys ?? 0}
                  </div>
                </div>
                <div>
                  <div className="text-dark-400">{t('plugins.block_radar.settings.qcode_today_cost')}</div>
                  <div className="text-white font-mono mt-1">
                    {qcodeUsage.data.account?.formatted_today_cost ?? '—'}
                  </div>
                </div>
                <div>
                  <div className="text-dark-400">{t('plugins.block_radar.settings.qcode_health')}</div>
                  <div className={`font-mono mt-1 ${qcodeUsage.data.account?.has_any_errors ? 'text-amber-300' : 'text-emerald-300'}`}>
                    {qcodeUsage.data.account?.has_any_errors
                      ? t('plugins.block_radar.settings.qcode_has_errors')
                      : t('plugins.block_radar.settings.qcode_ok')}
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                {qcodeUsage.data.keys.map((key, index) => (
                  <div key={`${key.name ?? 'key'}-${index}`} className="rounded-lg border border-white/10 bg-black/10 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-xs font-medium text-white">{key.name ?? t('plugins.block_radar.settings.qcode_key')}</div>
                      <span className={`text-[10px] ${key.is_active ? 'text-emerald-300' : 'text-dark-400'}`}>
                        {key.is_active ? t('plugins.block_radar.settings.qcode_active') : t('plugins.block_radar.settings.qcode_inactive')}
                      </span>
                    </div>
                    <div className="mt-2 grid gap-2 sm:grid-cols-4 text-[11px]">
                      <div><span className="text-dark-400">{t('plugins.block_radar.settings.qcode_expires')}</span><div className="text-white mt-0.5">{key.expires_at_display ?? key.expires_at ?? '—'}</div></div>
                      <div><span className="text-dark-400">{t('plugins.block_radar.settings.qcode_key_today')}</span><div className="text-white font-mono mt-0.5">{key.formatted_current_cost ?? '—'}{key.daily_cost_limit != null ? ` / $${key.daily_cost_limit}` : ''}</div></div>
                      <div><span className="text-dark-400">{t('plugins.block_radar.settings.qcode_requests')}</span><div className="text-white font-mono mt-0.5">{key.current_requests.toLocaleString()}</div></div>
                      <div><span className="text-dark-400">{t('plugins.block_radar.settings.qcode_tokens')}</span><div className="text-white font-mono mt-0.5">{key.current_tokens.toLocaleString()}</div></div>
                    </div>
                    {(key.is_near_cost_limit || key.is_near_opus_limit || key.has_error) && (
                      <p className="mt-2 text-[11px] text-amber-300">
                        {t('plugins.block_radar.settings.qcode_warning')}{key.error_code ? `: ${key.error_code}` : ''}
                      </p>
                    )}
                  </div>
                ))}
              </div>
              <p className="text-[10px] text-dark-400">{t('plugins.block_radar.settings.qcode_read_only')}</p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}


function BackLink() {
  const { t } = useTranslation()
  return (
    <Link
      to="/plugins/block-radar"
      className="inline-flex items-center gap-2 text-sm text-dark-300 hover:text-white transition-colors"
    >
      <ArrowLeft className="w-4 h-4" />
      {t('plugins.block_radar.settings.back')}
    </Link>
  )
}
