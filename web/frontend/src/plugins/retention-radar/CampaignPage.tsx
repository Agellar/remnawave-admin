/**
 * /plugins/retention-radar/campaign/:key — запуск кампании по сегменту.
 *
 * Порядок намеренно неудобный для случайной отправки: сначала предпросмотр
 * с текстом и составом аудитории, потом пробный прогон, и только третьим
 * шагом — реальная отправка с отдельным подтверждением. Сообщение уходит
 * живым людям, откатить его нельзя.
 *
 * Правка текста сбрасывает подтверждение: на сервере отправка сверяется с
 * отпечатком того, что оператор реально видел.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, ArrowLeft, Loader2, Send, Sparkles } from '@/components/brand/icons'

import LicenseBanner from '@/components/plugins/license'
import { armCampaign, asLicenseError, previewCampaign, sendCampaign } from './api'
import { Skeleton } from './primitives'
import { SEGMENT_KEYS, type CampaignArm, type CampaignPreview, type SegmentKey } from './types'

export default function CampaignPage() {
  const { t } = useTranslation()
  const { key = '' } = useParams<{ key: string }>()
  const segmentKey = SEGMENT_KEYS.includes(key as SegmentKey) ? (key as SegmentKey) : null

  const [text, setText] = useState('')
  const [armed, setArmed] = useState<CampaignArm | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const { data, isLoading, error, refetch, isFetching } = useQuery<CampaignPreview>({
    queryKey: ['retention-radar-campaign', segmentKey],
    queryFn: () => previewCampaign(segmentKey as SegmentKey),
    enabled: segmentKey !== null,
    retry: false,
    staleTime: 0,
  })

  useEffect(() => {
    if (data) setText(data.message_text)
  }, [data])

  // Текст изменили — подтверждение больше не относится к тому, что увидит
  // получатель. Взводить «отправить по-настоящему» приходится заново.
  const dirty = data ? text.trim() !== data.message_text.trim() : false
  useEffect(() => {
    if (dirty) setArmed(null)
  }, [dirty])

  const armMutation = useMutation({
    mutationFn: () => armCampaign(data?.confirm_token ?? ''),
    onSuccess: (value) => {
      setArmed(value)
      toast.warning(
        t('plugins.retention_radar.campaign.armed_server', {
          minutes: Math.round(value.expires_in_seconds / 60),
          defaultValue: `Сервер разрешил одну отправку на ${Math.round(value.expires_in_seconds / 60)} мин.`,
        }),
      )
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(
        detail === 'live_campaigns_disabled'
          ? t('plugins.retention_radar.campaign.live_disabled', {
              defaultValue: 'Реальные кампании выключены в настройках безопасности.',
            })
          : t('plugins.retention_radar.campaign.arm_failed', {
              defaultValue: 'Не удалось получить серверное разрешение.',
            }),
      )
    },
  })

  const mutation = useMutation({
    mutationFn: (dryRun: boolean) =>
      sendCampaign({
        segment: segmentKey as SegmentKey,
        message_text: text.trim(),
        confirm_token: data?.confirm_token ?? '',
        dry_run: dryRun,
        arm_token: dryRun ? undefined : armed?.arm_token,
        idempotency_key: dryRun ? undefined : armed?.idempotency_key,
      }),
    onSuccess: (res) => {
      setArmed(null)
      if (res.dry_run) {
        const dryRunMessage = t('plugins.retention_radar.campaign.dry_run_done', {
          n: res.recipients,
          throttled: res.skipped_throttled,
        })
        setResult(dryRunMessage)
        toast.success(dryRunMessage)
      } else {
        setResult(
          t('plugins.retention_radar.campaign.sent_done', {
            n: res.recipients,
            offers: res.offers_created,
          }),
        )
        toast.success(t('plugins.retention_radar.campaign.sent_toast'))
        refetch()
      }
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      if (detail === 'stale_confirmation') {
        toast.error(t('plugins.retention_radar.campaign.stale'))
        refetch()
      } else {
        toast.error(t('plugins.retention_radar.campaign.failed', { reason: detail ?? '' }))
      }
    },
  })

  if (!segmentKey) return null
  const licenseError = error ? asLicenseError(error) : null
  if (licenseError) return <LicenseBanner error={licenseError} />
  if (isLoading || !data) return <Skeleton className="h-96 w-full" />

  const skipped = data.skipped
  const skippedTotal =
    skipped.no_telegram + skipped.cooldown + skipped.over_limit + skipped.active_incident +
    skipped.bedolaga_auto + skipped.throttled

  return (
    <div className="space-y-6 max-w-3xl">
      <Link
        to={`/plugins/retention-radar/segment/${segmentKey}`}
        className="inline-flex items-center gap-1.5 text-xs text-dark-300 hover:text-white"
      >
        <ArrowLeft className="w-3.5 h-3.5" aria-hidden />
        {t('plugins.retention_radar.campaign.back')}
      </Link>

      <div>
        <h1 className="text-xl font-semibold text-white">
          {t('plugins.retention_radar.campaign.title', {
            segment: t(`plugins.retention_radar.segments.${segmentKey}.title`),
          })}
        </h1>
        <p className="mt-1 text-sm text-dark-300">
          {t('plugins.retention_radar.campaign.summary', {
            n: data.recipients,
            percent: data.discount_percent,
            hours: data.offer_valid_hours,
          })}
        </p>
      </div>

      {/* Кого отсеяли и почему: «в сегменте 90, писать будем 61» без
          объяснения выглядит как потеря людей. */}
      {skippedTotal > 0 && (
        <div className="glass-card p-4 text-xs text-dark-300 space-y-1">
          <p className="text-dark-200">{t('plugins.retention_radar.campaign.skipped_title')}</p>
          {skipped.bedolaga_auto > 0 && (
            <p>
              {t('plugins.retention_radar.campaign.skip_bedolaga', { n: skipped.bedolaga_auto })}
            </p>
          )}
          {skipped.no_telegram > 0 && (
            <p>{t('plugins.retention_radar.campaign.skip_no_telegram', { n: skipped.no_telegram })}</p>
          )}
          {skipped.cooldown > 0 && (
            <p>{t('plugins.retention_radar.campaign.skip_cooldown', { n: skipped.cooldown })}</p>
          )}
          {skipped.active_incident > 0 && (
            <p>
              {t('plugins.retention_radar.campaign.skip_incident', {
                n: skipped.active_incident,
                defaultValue: `Активный инфраструктурный инцидент: ${skipped.active_incident}`,
              })}
            </p>
          )}
          {skipped.throttled > 0 && (
            <p>{t('plugins.retention_radar.campaign.skip_throttled', { n: skipped.throttled })}</p>
          )}
          {skipped.over_limit > 0 && (
            <p>{t('plugins.retention_radar.campaign.skip_limit', { n: skipped.over_limit })}</p>
          )}
          {skipped.bedolaga_auto > 0 && (
            <p className="pt-1 text-dark-400">
              {t('plugins.retention_radar.campaign.quarantine_note')}
            </p>
          )}
        </div>
      )}

      {/* Пустая аудитория — штатный исход, а не поломка: значит по этим
          людям уже работает бот. Объясняем, иначе выглядит как баг. */}
      {data.recipients === 0 && (
        <div className="glass-card p-4 text-sm text-dark-200">
          {t('plugins.retention_radar.campaign.empty_audience')}
        </div>
      )}

      <div className="glass-card p-5 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-dark-300">
            <Sparkles className="w-3.5 h-3.5" aria-hidden />
            {t('plugins.retention_radar.campaign.text_title')}
          </div>
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="text-[11px] px-2 py-1 rounded border border-[var(--glass-border)] text-dark-200 hover:text-white hover:bg-[var(--glass-bg)] disabled:opacity-40"
          >
            {t('plugins.retention_radar.campaign.regenerate')}
          </button>
        </div>

        {data.ai_error && (
          <p className="text-xs text-amber-400">
            {t('plugins.retention_radar.campaign.ai_failed', { reason: data.ai_error })}
          </p>
        )}

        {/* Обещания, которых система не выполнит. Проверяется и текст
            модели, и правка оператора — ошибиться может любой. */}
        {data.warnings.length > 0 && (
          <div className="rounded border border-amber-500/40 bg-amber-500/[0.06] p-3 text-xs text-amber-300">
            <p className="font-medium">{t('plugins.retention_radar.campaign.warnings_title')}</p>
            <p className="mt-1 text-amber-200/80">
              {t('plugins.retention_radar.campaign.warnings_hint')}
            </p>
            <ul className="mt-1.5 list-disc pl-4">
              {data.warnings.map((phrase) => (
                <li key={phrase}>«{phrase}»</li>
              ))}
            </ul>
          </div>
        )}

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          maxLength={4000}
          className="w-full rounded border border-[var(--glass-border)] bg-[var(--glass-bg)] p-3 text-sm text-dark-100 leading-relaxed"
        />
        <p className="text-[11px] text-dark-400">
          {t('plugins.retention_radar.campaign.text_hint', { n: text.trim().length })}
        </p>

        {/* Кнопка — часть сообщения, оператор должен видеть и её. */}
        <div className="rounded border border-[var(--glass-border)] bg-[var(--glass-bg)] px-3 py-2 text-center text-sm text-dark-100">
          {data.button_label}
        </div>
        <p className="text-[11px] text-dark-400">
          {t('plugins.retention_radar.campaign.button_hint')}
        </p>
      </div>

      {result && <div className="glass-card p-4 text-sm text-emerald-300">{result}</div>}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => mutation.mutate(true)}
          disabled={mutation.isPending || dirty || !text.trim()}
          className="inline-flex items-center gap-2 rounded border border-[var(--glass-border)] px-4 py-2 text-sm text-dark-100 hover:bg-[var(--glass-bg)] disabled:opacity-40"
        >
          {mutation.isPending && <Loader2 className="w-4 h-4 animate-spin" aria-hidden />}
          {t('plugins.retention_radar.campaign.dry_run')}
        </button>

        {!armed ? (
          <button
            type="button"
            onClick={() => armMutation.mutate()}
            disabled={
              dirty || !text.trim() || data.recipients === 0 || armMutation.isPending
            }
            className="inline-flex items-center gap-2 rounded border border-amber-500/40 px-4 py-2 text-sm text-amber-300 hover:bg-amber-500/10 disabled:opacity-40"
          >
            <Send className="w-4 h-4" aria-hidden />
            {armMutation.isPending
              ? t('plugins.retention_radar.campaign.arming', {
                  defaultValue: 'Получаю разрешение…',
                })
              : t('plugins.retention_radar.campaign.arm')}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => mutation.mutate(false)}
            disabled={mutation.isPending}
            className="inline-flex items-center gap-2 rounded bg-amber-500/90 px-4 py-2 text-sm text-black font-medium hover:bg-amber-500 disabled:opacity-50"
          >
            {mutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
            ) : (
              <AlertTriangle className="w-4 h-4" aria-hidden />
            )}
            {t('plugins.retention_radar.campaign.confirm', { n: data.recipients })}
          </button>
        )}

        {dirty && (
          <span className="text-xs text-dark-400">
            {t('plugins.retention_radar.campaign.dirty')}
          </span>
        )}
      </div>

      <div className="glass-card p-5">
        <p className="text-[11px] uppercase tracking-wider text-dark-300 mb-3">
          {t('plugins.retention_radar.campaign.sample_title')}
        </p>
        <ul className="space-y-1.5 text-sm text-dark-300">
          {data.sample.map((user) => (
            <li key={user.uuid} className="flex items-center gap-2">
              <span className="text-dark-100">{user.username || user.uuid.slice(0, 8)}</span>
              <span className="text-xs text-dark-400">tg: {user.telegram_id ?? '—'}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
