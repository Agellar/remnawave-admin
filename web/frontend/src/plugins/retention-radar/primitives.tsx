/**
 * Мелкие визуальные детали плагина: скелеты, пустые состояния, спарклайн
 * и плитка сегмента. Держим в одном файле — они крошечные и нужны везде.
 */
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Inbox, TrendingDown, TrendingUp, type LucideIcon } from '@/components/brand/icons'

import type { SegmentCard, SegmentKey, TrendPoint } from './types'

export function Skeleton({ className = '' }: { className?: string }) {
  return (
    <div
      className={
        'animate-pulse rounded bg-[var(--glass-bg)] border border-[var(--glass-border)] ' +
        className
      }
    />
  )
}

export function EmptyState({
  icon: Icon = Inbox,
  message,
  hint,
}: {
  icon?: LucideIcon
  message: string
  hint?: string
}) {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="rounded-full bg-[var(--glass-bg)] p-3 mb-3">
        <Icon className="w-5 h-5 text-dark-300" aria-hidden />
      </div>
      <p className="text-sm text-dark-200">{message}</p>
      {hint && <p className="mt-1 text-xs text-dark-400 max-w-sm">{hint}</p>}
    </div>
  )
}

/**
 * Спарклайн без библиотек: точек мало (две недели), а тянуть в бандл
 * charting-движок ради ломаной — перебор.
 */
export function Sparkline({ points }: { points: TrendPoint[] }) {
  if (points.length < 2) return null

  const values = points.map((p) => p.users_count)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const step = 100 / (points.length - 1)

  const path = values
    .map((v, i) => `${i * step},${28 - ((v - min) / span) * 24}`)
    .join(' ')

  return (
    <svg
      viewBox="0 0 100 28"
      preserveAspectRatio="none"
      className="w-full h-7 mt-3"
      aria-hidden
    >
      <polyline
        points={path}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        vectorEffect="non-scaling-stroke"
        className="text-primary-400/70"
      />
    </svg>
  )
}

/**
 * Плитка сегмента. Рост числа людей в сегменте оттока — это плохо,
 * поэтому стрелка вверх красная, а вниз зелёная: цвет отражает смысл,
 * а не направление.
 */
export function SegmentTile({ card }: { card: SegmentCard }) {
  const { t } = useTranslation()
  const delta = card.delta ?? null
  const Arrow = delta !== null && delta < 0 ? TrendingDown : TrendingUp

  return (
    <Link
      to={`/plugins/retention-radar/segment/${card.key}`}
      className="glass-card p-5 block transition-colors hover:border-[var(--glass-border-hover)]"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-wider text-dark-300">
            {t(`plugins.retention_radar.segments.${card.key}.title`)}
          </p>
          <p className="mt-2 text-2xl font-semibold text-white tabular-nums">
            {card.users_count}
          </p>
        </div>
        {delta !== null && delta !== 0 && (
          <span
            className={
              'inline-flex items-center gap-1 text-xs tabular-nums ' +
              (delta > 0 ? 'text-amber-400' : 'text-emerald-400')
            }
          >
            <Arrow className="w-3.5 h-3.5" aria-hidden />
            {delta > 0 ? `+${delta}` : delta}
          </span>
        )}
      </div>
      <p className="mt-1 text-xs text-dark-400 leading-snug">
        {t(`plugins.retention_radar.segments.${card.key}.hint`)}
      </p>
      <Sparkline points={card.trend} />
    </Link>
  )
}

export function segmentTitleKey(key: SegmentKey): string {
  return `plugins.retention_radar.segments.${key}.title`
}
