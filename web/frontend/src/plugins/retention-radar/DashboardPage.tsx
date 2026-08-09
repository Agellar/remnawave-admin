/**
 * /plugins/retention-radar — обзор оттока.
 *
 * Сверху четыре плитки сегментов с динамикой, ниже — «горит прямо
 * сейчас»: люди, у которых подписка кончается на днях, а они уже молчат.
 * Это единственный список, который стоит открывать каждый день, поэтому
 * он на главной, а не за кликом.
 */
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Settings, Stethoscope } from '@/components/brand/icons'

import LicenseBanner from '@/components/plugins/license'
import { asLicenseError, fetchOverview } from './api'
import { EmptyState, SegmentTile, Skeleton } from './primitives'
import { SEGMENT_KEYS } from './types'

export default function DashboardPage() {
  const { t } = useTranslation()

  const { data, isLoading, error } = useQuery({
    queryKey: ['retention-radar-overview'],
    queryFn: fetchOverview,
    retry: false,
    staleTime: 60_000,
  })

  const licenseError = useMemo(() => (error ? asLicenseError(error) : null), [error])
  if (licenseError) return <LicenseBanner error={licenseError} />

  if (isLoading || !data) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-64" />
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {SEGMENT_KEYS.map((key) => (
            <Skeleton key={key} className="h-40 w-full" />
          ))}
        </div>
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="glass-card p-6 text-sm text-amber-300">
        {t('plugins.retention_radar.error')}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">
            {t('plugins.retention_radar.title')}
          </h1>
          <p className="mt-1 text-sm text-dark-300">
            {t('plugins.retention_radar.subtitle', {
              total: data.total_users,
              active: data.active_users,
            })}
          </p>
        </div>
        <Link
          to="/plugins/retention-radar/settings"
          className="inline-flex items-center gap-1.5 text-xs text-dark-200 hover:text-white px-2.5 py-1.5 rounded border border-[var(--glass-border)] hover:bg-[var(--glass-bg)]"
        >
          <Settings className="w-3.5 h-3.5" aria-hidden />
          {t('plugins.retention_radar.settings.link')}
        </Link>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {data.segments.map((card) => (
          <SegmentTile key={card.key} card={card} />
        ))}
      </div>

      {!data.data_freshness.fresh && (
        <div className="glass-card border border-amber-500/30 p-4 text-sm text-amber-200">
          {t('plugins.retention_radar.data_stale', {
            age: data.data_freshness.age_seconds == null
              ? '—'
              : Math.round(data.data_freshness.age_seconds / 60),
            limit: data.data_freshness.max_age_minutes,
          })}
        </div>
      )}

      {/* Пока история срезов короче двух дней, дельты и спарклайны пустые —
          честнее сказать об этом, чем оставить пользователя гадать. */}
      {data.segments.every((s) => s.trend.length < 2) && (
        <p className="text-xs text-dark-400">
          {t('plugins.retention_radar.history_warming', {
            since: data.history_since ?? '—',
          })}
        </p>
      )}

      <div className="glass-card p-5">
        <div className="flex items-center gap-2 mb-4">
          <AlertTriangle className="w-4 h-4 text-amber-400" aria-hidden />
          <h2 className="text-sm font-semibold text-white uppercase tracking-wider">
            {t('plugins.retention_radar.attention.title')}
          </h2>
        </div>

        {data.attention.length === 0 ? (
          <EmptyState
            message={t('plugins.retention_radar.attention.empty')}
            hint={t('plugins.retention_radar.attention.empty_hint')}
          />
        ) : (
          <ul className="divide-y divide-[var(--glass-border)]">
            {data.attention.map((user) => (
              <li key={user.uuid} className="py-2.5 flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-dark-100 truncate">
                    {user.username || user.uuid.slice(0, 8)}
                  </p>
                  <p className="text-xs text-dark-400">
                    {t('plugins.retention_radar.attention.row', {
                      days: user.days_until_expire ?? 0,
                      silent:
                        user.days_silent === null || user.days_silent === undefined
                          ? t('plugins.retention_radar.never_online')
                          : t('plugins.retention_radar.days', { n: user.days_silent }),
                    })}
                  </p>
                </div>
                {/* Мост в Smart Support: оттуда видно, почему человек замолчал. */}
                <Link
                  to={`/plugins/smart-support/report/${user.uuid}`}
                  title={t('plugins.retention_radar.open_report')}
                  className="shrink-0 p-1.5 rounded text-dark-300 hover:text-white hover:bg-[var(--glass-bg)]"
                >
                  <Stethoscope className="w-4 h-4" aria-hidden />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
