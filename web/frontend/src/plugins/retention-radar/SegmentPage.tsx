/**
 * /plugins/retention-radar/segment/:key — список людей одного сегмента.
 *
 * Таблица, а не карточки: колонки здесь сравнивают между собой (кто молчит
 * дольше, у кого раньше сгорит подписка), а карточки такое сравнение
 * ломают. Из каждой строки — переход в отчёт Smart Support.
 */
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeft, Download, Send, Stethoscope } from '@/components/brand/icons'

import LicenseBanner from '@/components/plugins/license'
import { formatDateUtil } from '@/lib/useFormatters'
import { asLicenseError, downloadSegmentCsv, fetchSegment } from './api'
import { EmptyState, Skeleton } from './primitives'
import { SEGMENT_KEYS, type SegmentKey } from './types'

const PAGE_SIZE = 50

export default function SegmentPage() {
  const { t } = useTranslation()
  const { key = '' } = useParams<{ key: string }>()
  const [offset, setOffset] = useState(0)
  const [downloading, setDownloading] = useState(false)

  const segmentKey = SEGMENT_KEYS.includes(key as SegmentKey) ? (key as SegmentKey) : null

  const { data, isLoading, error } = useQuery({
    queryKey: ['retention-radar-segment', segmentKey, offset],
    queryFn: () => fetchSegment(segmentKey as SegmentKey, PAGE_SIZE, offset),
    enabled: segmentKey !== null,
    retry: false,
    staleTime: 60_000,
  })

  const licenseError = useMemo(() => (error ? asLicenseError(error) : null), [error])

  const onExport = async () => {
    if (!segmentKey) return
    setDownloading(true)
    try {
      await downloadSegmentCsv(segmentKey)
    } catch {
      toast.error(t('plugins.retention_radar.export_failed'))
    } finally {
      setDownloading(false)
    }
  }

  if (!segmentKey) {
    return <EmptyState message={t('plugins.retention_radar.unknown_segment')} />
  }
  if (licenseError) return <LicenseBanner error={licenseError} />

  const shown = (data?.users ?? []).length
  const total = data?.total ?? 0

  return (
    <div className="space-y-6">
      <Link
        to="/plugins/retention-radar"
        className="inline-flex items-center gap-1.5 text-xs text-dark-300 hover:text-white"
      >
        <ArrowLeft className="w-3.5 h-3.5" aria-hidden />
        {t('plugins.retention_radar.back')}
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">
            {t(`plugins.retention_radar.segments.${segmentKey}.title`)}
          </h1>
          <p className="mt-1 text-sm text-dark-300">
            {t(`plugins.retention_radar.segments.${segmentKey}.hint`)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onExport}
            disabled={downloading || total === 0}
            className="inline-flex items-center gap-1.5 text-xs text-dark-200 hover:text-white px-2.5 py-1.5 rounded border border-[var(--glass-border)] hover:bg-[var(--glass-bg)] disabled:opacity-40"
          >
            <Download className="w-3.5 h-3.5" aria-hidden />
            {t('plugins.retention_radar.export')}
          </button>
          <Link
            to={`/plugins/retention-radar/campaign/${segmentKey}`}
            className="inline-flex items-center gap-1.5 text-xs text-amber-300 hover:text-amber-200 px-2.5 py-1.5 rounded border border-amber-500/30 hover:bg-amber-500/10"
          >
            <Send className="w-3.5 h-3.5" aria-hidden />
            {t('plugins.retention_radar.campaign.start')}
          </Link>
        </div>
      </div>

      {isLoading && !data ? (
        <Skeleton className="h-80 w-full" />
      ) : total === 0 ? (
        <div className="glass-card p-5">
          <EmptyState
            message={t('plugins.retention_radar.segment_empty')}
            hint={t('plugins.retention_radar.segment_empty_hint')}
          />
        </div>
      ) : (
        <div className="glass-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-dark-400">
                <th className="px-4 py-3 font-medium">
                  {t('plugins.retention_radar.columns.user')}
                </th>
                <th className="px-4 py-3 font-medium">
                  {t('plugins.retention_radar.columns.last_online')}
                </th>
                <th className="px-4 py-3 font-medium">
                  {t('plugins.retention_radar.columns.expire')}
                </th>
                <th className="px-4 py-3 font-medium">
                  {t('plugins.retention_radar.columns.telegram')}
                </th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--glass-border)]">
              {data?.users.map((user) => (
                <tr
                  key={user.uuid}
                  className={user.at_risk ? 'bg-amber-500/[0.04]' : undefined}
                >
                  <td className="px-4 py-2.5">
                    <span className="text-dark-100">
                      {user.username || user.uuid.slice(0, 8)}
                    </span>
                    {user.at_risk && (
                      <span className="ml-2 text-[10px] uppercase tracking-wider text-amber-400">
                        {t('plugins.retention_radar.at_risk')}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-dark-300 tabular-nums">
                    {user.days_silent === null || user.days_silent === undefined
                      ? t('plugins.retention_radar.never_online')
                      : t('plugins.retention_radar.days_ago', { n: user.days_silent })}
                  </td>
                  <td className="px-4 py-2.5 text-dark-300 tabular-nums">
                    {user.expire_at ? formatDateUtil(user.expire_at) : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-dark-300 tabular-nums">
                    {user.telegram_id ? (
                      <a
                        href={`tg://user?id=${user.telegram_id}`}
                        className="inline-flex items-center gap-1 hover:text-white"
                      >
                        <Send className="w-3 h-3" aria-hidden />
                        {user.telegram_id}
                      </a>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <Link
                      to={`/plugins/smart-support/report/${user.uuid}`}
                      title={t('plugins.retention_radar.open_report')}
                      className="inline-flex p-1.5 rounded text-dark-300 hover:text-white hover:bg-[var(--glass-bg)]"
                    >
                      <Stethoscope className="w-4 h-4" aria-hidden />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-xs text-dark-300">
          <span>
            {t('plugins.retention_radar.pagination', {
              from: offset + 1,
              to: offset + shown,
              total,
            })}
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="px-2.5 py-1.5 rounded border border-[var(--glass-border)] hover:bg-[var(--glass-bg)] disabled:opacity-40"
            >
              {t('plugins.retention_radar.prev')}
            </button>
            <button
              type="button"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={offset + shown >= total}
              className="px-2.5 py-1.5 rounded border border-[var(--glass-border)] hover:bg-[var(--glass-bg)] disabled:opacity-40"
            >
              {t('plugins.retention_radar.next')}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
